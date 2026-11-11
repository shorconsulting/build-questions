import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import bq  # noqa: E402

ASK_INPUT = {
    "questions": [
        {"question": "Which database should the app use?", "header": "Database", "multiSelect": False,
         "options": [{"label": "Postgres (Recommended)", "description": "Managed, relational"},
                     {"label": "SQLite", "description": "File on disk"}]},
        {"question": "Which sign-in methods?", "header": "Auth", "multiSelect": True,
         "options": [{"label": "Email link", "description": ""}, {"label": "Google", "description": ""}]},
    ]
}


def run(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        bq.main(argv)
    return out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.home = t / "home"
        self.root = t / "app"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "remote", "add", "origin",
                        "https://git.example.com/acme/app.git"], check=True)
        self.user_home = t / "user"
        # Point the plugin's folders at the temp dir.
        self.patches = [mock.patch.object(bq, "home", lambda: self.home),
                        mock.patch.object(bq.Path, "home", classmethod(lambda cls: self.user_home))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def enroll(self):
        return json.loads(run(["enroll", "--root", str(self.root)]))

    def hook(self, fn, payload, as_repo_copy=True):
        """Hooks run as the repo's committed copy by default (as the repo's own hooks do)."""
        out = io.StringIO()
        here = (self.root / bq.TOOL_REL).resolve() if as_repo_copy else bq.HERE
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out), \
                mock.patch.object(bq, "HERE", here):
            fn(None)
        return out.getvalue()

    def ask_payload(self, **kw):
        p = {"tool_name": "AskUserQuestion", "tool_input": ASK_INPUT, "session_id": "s1",
             "tool_use_id": "toolu_9", "cwd": str(self.root),
             "tool_response": {"answers": {"Which database should the app use?": "SQLite"}}}
        p.update(kw)
        return p

    def store(self):
        return bq.read_store(self.root, "acme/app")


class TestRemote(unittest.TestCase):
    def test_remotes(self):
        cases = {
            "https://git.example.com/shorconsulting/marketplace.git": "shorconsulting/marketplace",
            "git@git.example.com:ShorConsultingInc/tidyhaus.git": "ShorConsultingInc/tidyhaus",
            "http://local_proxy@127.0.0.1:1234/git/owner/repo": "owner/repo",
            "ssh://git@git.example.com/a/b/": "a/b",
        }
        for url, want in cases.items():
            self.assertEqual(bq.repo_from_remote(url), want, url)
        self.assertIsNone(bq.repo_from_remote(""))


class TestParsing(unittest.TestCase):
    def test_answers_in_response_dict(self):
        resp = {"answers": {"Which database should the app use?": "Postgres (Recommended)",
                            "Which sign-in methods?": "Email link, Google"},
                "annotations": {"Which database should the app use?": {"notes": "Neon is fine"}}}
        recs = bq.records_from_ask(ASK_INPUT, resp, repo="o/r", tool_use_id="toolu_1")
        self.assertEqual([r["answer"] for r in recs], ["Postgres (Recommended)", "Email link, Google"])
        self.assertEqual(recs[0]["notes"], "Neon is fine")
        self.assertFalse(recs[1]["custom_answer"])

    def test_answers_in_string_response(self):
        resp = 'User has answered your questions: "Which database should the app use?"="Something else: Mongo".'
        recs = bq.records_from_ask(ASK_INPUT, resp, repo="o/r", tool_use_id="toolu_2")
        self.assertEqual(recs[0]["answer"], "Something else: Mongo")
        self.assertTrue(recs[0]["custom_answer"])
        self.assertEqual(recs[1]["status"], "unanswered")

    def test_ids_stable_per_tool_use(self):
        a = bq.records_from_ask(ASK_INPUT, {}, repo="o/r", tool_use_id="x", asked_at="2026-01-01T00:00:00Z")
        b = bq.records_from_ask(ASK_INPUT, {}, repo="o/r", tool_use_id="x", asked_at="2026-02-01T00:00:00Z")
        self.assertEqual([r["id"] for r in a], [r["id"] for r in b])


class TestMerge(unittest.TestCase):
    def rec(self, q, a="", at="2026-10-01T00:00:00Z", **kw):
        return bq.clean({"question": q, "answer": a, "asked_at": at, **kw}, "o/r")

    def test_similar_questions_merge_newer_answer_wins(self):
        a = self.rec("Which database should the app use?", "SQLite", "2026-10-01T00:00:00Z")
        b = self.rec("Which database should this app use?", "Postgres", "2026-10-05T00:00:00Z",
                     sources=[{"type": "file", "path": "docs/plan.md"}])
        recs, stats = bq.merge_records([a], [b])
        self.assertEqual(len(recs), 1)
        self.assertEqual(stats["merged"], 1)
        r = recs[0]
        self.assertEqual(r["answer"], "Postgres")
        self.assertEqual(r["history"][0]["answer"], "SQLite")
        self.assertEqual(r["status"], "changed")
        self.assertIn(b["id"], r["aliases"])
        self.assertEqual(len(r["sources"]), 2)

    def test_older_answer_does_not_overwrite_but_fills_blank(self):
        a = self.rec("Pick a colour for the button?", "", "2026-10-05T00:00:00Z")
        b = self.rec("Pick a colour for the button?", "Orange", "2026-10-01T00:00:00Z")
        recs, _ = bq.merge_records([a], [b])
        self.assertEqual(recs[0]["answer"], "Orange")
        self.assertEqual(recs[0]["asked_at"], "2026-10-01T00:00:00Z")
        c = self.rec("Pick a colour for the button?", "Green", "2026-09-01T00:00:00Z")
        recs, _ = bq.merge_records(recs, [c])
        self.assertEqual(recs[0]["answer"], "Orange")

    def test_different_questions_stay_apart_and_numbering(self):
        recs, _ = bq.merge_records([], [self.rec("Which database?", at="2026-10-02T00:00:00Z"),
                                        self.rec("Which hosting provider?", at="2026-10-01T00:00:00Z")])
        self.assertEqual([(r["n"], r["question"]) for r in recs], [(1, "Which hosting provider?"), (2, "Which database?")])

    def test_duplicate_numbers_from_branch_merge_are_fixed(self):
        a = self.rec("Question one?", n=3)
        b = self.rec("Totally different question two?", n=3, at="2026-10-02T00:00:00Z")
        recs = bq.renumber([a, b])
        self.assertEqual(sorted(r["n"] for r in recs), [3, 4])


class TestStore(Base):
    def test_enroll_creates_everything_idempotently(self):
        res = self.enroll()
        self.assertTrue(res["created"])
        self.assertEqual(res["claude_md"], "created")
        d = self.root / ".claude" / "build-questions"
        for f in ("questions.jsonl", "QUESTIONS.md", "README.md"):
            self.assertTrue((d / f).exists(), f)
        self.assertIn("merge=union", (self.root / ".gitattributes").read_text(encoding="utf-8"))
        res2 = self.enroll()
        self.assertEqual(res2["claude_md"], "unchanged")
        self.assertFalse(res2["gitattributes_changed"])
        self.assertEqual((self.root / "CLAUDE.md").read_text(encoding="utf-8").count(bq.MEM_START), 1)

    def test_memory_block_replaced_in_place(self):
        (self.root / "CLAUDE.md").write_text("# App\n\nOwn notes.\n\n" + bq.MEM_START + "\nold\n" + bq.MEM_END + "\n\nMore.\n")
        self.enroll()
        txt = (self.root / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("Own notes.", txt)
        self.assertIn("More.", txt)
        self.assertNotIn("\nold\n", txt)
        self.assertIn("questions.jsonl", txt)

    def test_user_memory(self):
        res = json.loads(run(["memory", "--user", "--root", str(self.root)]))
        self.assertEqual(res, {"user": "created"})
        self.assertIn("build-questions", (self.user_home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_hand_appended_line_without_id_is_read(self):
        self.enroll()
        with bq.store_file(self.root).open("a") as f:
            f.write(json.dumps({"question": "Do we need dark mode?", "answer": "Yes"}) + "\n")
        recs = self.store()
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["id"].startswith("q-"))
        self.assertEqual(recs[0]["n"], 1)

    def test_history_entry_without_answer_renders(self):
        self.enroll()
        p = self.root / ".claude" / "build-questions" / "questions.jsonl"
        p.write_text(json.dumps({"question": "Which DB?", "answer": "Postgres", "history": [
            {"at": "2026-10-01T00:00:00Z", "question": "DB?"}, {"at": "2026-10-02T00:00:00Z", "answer": "MySQL"}]}) + "\n")
        self.enroll()
        self.assertIn("(was: ~~MySQL~~)", (p.parent / "QUESTIONS.md").read_text(encoding="utf-8"))

    def test_union_merge_duplicates_latest_wins(self):
        self.enroll()
        base = {"id": "q-1", "question": "Which database?", "answer": "SQLite", "asked_at": "2026-10-01T00:00:00Z", "n": 1}
        newer = dict(base, answer="Postgres", updated_at="2026-10-03T00:00:00Z")
        bq.store_file(self.root).write_text(json.dumps(newer) + "\n" + json.dumps(base) + "\n")
        self.assertEqual(self.store()[0]["answer"], "Postgres")

    def test_non_ascii_round_trip_and_lf_line_endings(self):
        self.enroll()
        run(["record", "--root", str(self.root), "--question", "Café menu: which currency (€ or ¥)?",
             "--answer", "Euro — €", "--topic", "Préférences"])
        r = self.store()[0]
        self.assertEqual((r["question"], r["answer"]), ("Café menu: which currency (€ or ¥)?", "Euro — €"))
        self.assertIn("Euro — €", (bq.store_dir(self.root) / "QUESTIONS.md").read_text(encoding="utf-8"))
        self.assertNotIn(b"\r\n", bq.store_file(self.root).read_bytes())
        self.assertNotIn(b"\r\n", (self.root / "CLAUDE.md").read_bytes())

    def test_record_and_set_answer(self):
        self.enroll()
        out = json.loads(run(["record", "--root", str(self.root), "--question", "Button colour?", "--answer", "Green",
                              "--topic", "Design"]))
        self.assertEqual(out["added"], 1)
        run(["set-answer", "--root", str(self.root), "Q001", "Orange", "--request", "r1"])
        r = self.store()[0]
        self.assertEqual((r["answer"], r["status"], r["history"][0]["answer"]), ("Orange", "changed", "Green"))
        self.assertIn("Orange", (bq.store_dir(self.root) / "QUESTIONS.md").read_text(encoding="utf-8"))


class TestFormats(Base):
    """Open and answered questions have different formats (David S, 11 Nov 2026)."""

    def test_open_format_problems(self):
        good = {"answer": "", "options": [{"label": "Recommended: Stripe", "description": ""}, {"label": "Square", "description": ""}],
                "notes": bq.open_notes("Stripe", "Least setup", "built today: none")}
        self.assertEqual(bq.open_format_problems(good), [])
        self.assertEqual(bq.open_format_problems({"answer": "Stripe"}), [])
        bare = {"answer": "", "options": [], "notes": "Open for the owner"}
        self.assertEqual(len(bq.open_format_problems(bare)), 3)
        self.assertTrue(bq.open_format_problems(dict(good, options=list(reversed(good["options"])))))

    def test_record_refuses_an_open_question_not_in_format(self):
        self.enroll()
        with self.assertRaises(SystemExit):
            run(["record", "--root", str(self.root), "--question", "Which provider takes card payments?"])
        self.assertEqual(self.store(), [])

    def test_record_builds_the_open_format(self):
        self.enroll()
        run(["record", "--root", str(self.root), "--question", "Which provider takes card payments?",
             "--options", "Square|Stripe|Braintree", "--recommendation", "Stripe", "--why", "Least setup.",
             "--notes", "Nothing is built yet"])
        r = self.store()[0]
        self.assertEqual([o["label"] for o in r["options"]], ["Recommended: Stripe", "Square", "Braintree"])
        self.assertTrue(r["notes"].startswith("Recommendation: Stripe. Why: Least setup. To answer, press Request a change"))
        self.assertTrue(r["notes"].endswith("| Nothing is built yet"))
        self.assertEqual((r["answer"], r["status"]), ("", "unanswered"))
        self.assertEqual(bq.open_format_problems(r), [])

    def test_lint_and_status_count_open_questions_not_in_format(self):
        self.enroll()
        with bq.store_file(self.root).open("a") as f:
            f.write(json.dumps({"question": "Dark mode?", "answer": ""}) + "\n")
            f.write(json.dumps({"question": "Which DB?", "answer": "Postgres"}) + "\n")
        out = io.StringIO()
        with redirect_stdout(out):
            code = bq.main(["lint", "--root", str(self.root)])
        res = json.loads(out.getvalue())
        self.assertEqual((code, res["open"], res["not_in_format"]), (1, 1, 1))
        self.assertEqual(json.loads(run(["status", "--root", str(self.root)]))["open_not_in_format"], 1)

    def test_memory_and_readme_carry_both_formats(self):
        self.enroll()
        for txt in ((self.root / "CLAUDE.md").read_text(encoding="utf-8"), (bq.store_dir(self.root) / "README.md").read_text(encoding="utf-8"),
                    bq.USER_MEMORY_BLOCK):
            self.assertIn("Recommended: ", txt)
            self.assertIn("Recommendation: ", txt)
            self.assertIn("written back", txt)
        readme = (bq.store_dir(self.root) / "README.md").read_text(encoding="utf-8")
        for block in readme.split("```json\n")[1:]:
            json.loads(block.split("\n```", 1)[0])  # every example line is valid JSON


class TestRepoHooks(Base):
    """0.7.0: the list's tool and hooks are committed in the repo, so every account and container runs them."""

    def test_enroll_commits_the_tool_and_hooks_and_keeps_other_settings(self):
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / "settings.json").write_text(json.dumps(
            {"permissions": {"allow": ["Bash(ls:*)"]},
             "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
        res = self.enroll()
        self.assertEqual(res["repo_hooks"], "updated")
        self.assertTrue((self.root / bq.TOOL_REL / "bq.py").exists())
        self.assertTrue((self.root / bq.TOOL_REL / "page.html").exists())
        cfg = json.loads((self.root / ".claude" / "settings.json").read_text())
        self.assertEqual(cfg["permissions"], {"allow": ["Bash(ls:*)"]})
        stop_cmds = [h["command"] for e in cfg["hooks"]["Stop"] for h in e["hooks"]]
        self.assertIn("echo mine", stop_cmds)
        self.assertEqual(sum(bq.HOOK_MARK in c for c in stop_cmds), 1)
        self.assertEqual(cfg["hooks"]["PreToolUse"][-1]["matcher"], "Write|Edit|MultiEdit|NotebookEdit")
        self.assertEqual(self.enroll()["repo_hooks"], "unchanged")  # idempotent, no duplicates

    def test_the_committed_copy_runs_on_its_own(self):
        self.enroll()
        out = subprocess.run([sys.executable, str(self.root / bq.TOOL_REL / "bq.py"), "lint", "--root", str(self.root)],
                             capture_output=True, text=True, env={**os.environ, "BUILD_QUESTIONS_HOME": str(self.home)})
        self.assertEqual(json.loads(out.stdout)["not_in_format"], 0)

    def test_plugin_hooks_stand_aside_where_the_repo_has_its_own(self):
        self.enroll()
        payload = {"cwd": str(self.root)}
        self.assertEqual(self.hook(bq.hook_session_start, payload, as_repo_copy=False), "")
        self.assertIn("nowhere else", self.hook(bq.hook_session_start, payload))

    def test_guard_refuses_questions_or_answers_outside_the_list(self):
        self.enroll()
        def guard(path, content):
            return self.hook(bq.hook_guard_write, {"cwd": str(self.root), "tool_name": "Write",
                                                   "tool_input": {"file_path": str(self.root / path), "content": content}})
        out = guard("docs/plan.md", "# Plan\n\n## Open questions\n\n- Which DB?\n")
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("questions.jsonl", out)
        self.assertTrue(guard("docs/Answers_2026-11-04.md", "his answers"))           # a new answers file, by name
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs" / "Answers_2026-10-28.md").write_text("Q1: Which DB?\nA: Postgres\n")
        self.assertEqual(guard("docs/Answers_2026-10-28.md",                         # organize's pointer line
                               "Moved to the central list: Q001 (answered). See ../.claude/build-questions/QUESTIONS.md"), "")
        self.assertTrue(guard("docs/Answers_2026-10-28.md", "Q2: Dark mode?\nA: Yes"))  # but no new answers in it
        self.assertTrue(guard("docs/answers_2026-11-04.json", "{}"))                  # an answer store kept as data
        self.assertEqual(guard("docs/plan.md", "# Plan\n\nBuild the thing.\n"), "")   # not a list of questions
        self.assertEqual(guard("src/app.py", "Q1: x"), "")                            # code is not checked
        self.assertEqual(guard(".claude/build-questions/notes.md", "## Open questions"), "")
        self.assertEqual(guard("docs/adr/0001-db.md", "Decision: Postgres"), "")      # ADRs stay where they are
        self.assertEqual(guard("docs/faq.md", "<!-- build-questions:allow -->\nQ: how?\nA: so"), "")
        (bq.store_dir(self.root) / "allow.json").write_text(json.dumps({"paths": ["docs/help/"]}))
        self.assertEqual(guard("docs/help/faq.md", "Q: how?"), "")

    def test_scan_finds_answer_files_and_pages(self):
        self.enroll()
        (self.root / "docs").mkdir()
        (self.root / "docs" / "Answers_2026-10-28.md").write_text("Everything he said.\n")
        (self.root / "docs" / "answers_2026-11-04.json").write_text("{}")
        (self.root / "docs" / "open-questions.html").write_text("<html></html>")
        (self.root / "docs" / "package.json").write_text("{}")
        found = {f["path"] for f in bq.scan_repo(self.root)}
        self.assertEqual(found, {"docs/Answers_2026-10-28.md", "docs/answers_2026-11-04.json", "docs/open-questions.html"})

    def test_stop_asks_to_merge_strays(self):
        self.enroll()
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], check=True)
        (self.root / "Answers.md").write_text("Q1: Which DB?\nA: Postgres\n")
        out = json.loads(self.hook(bq.hook_stop, {"cwd": str(self.root)}))
        self.assertEqual(out["decision"], "block")
        self.assertIn("Answers.md", out["reason"])

    def test_import_says_where_the_answers_now_are(self):
        self.enroll()
        run(["record", "--root", str(self.root), "--question", "Which database should bookings use?",
             "--options", "Postgres|SQLite", "--recommendation", "Postgres", "--why", "Managed."])
        cand = self.root / "cand.json"
        cand.write_text(json.dumps([
            {"question": "Which database should bookings use?", "answer": "Postgres",
             "source_ref": {"type": "file", "path": "docs/Answers.md", "line": 3}},
            {"question": "Do we need dark mode?", "answer": "",
             "source_ref": {"type": "file", "path": "docs/Answers.md", "line": 9}}]))
        res = json.loads(run(["import", "--root", str(self.root), "--file", str(cand)]))
        self.assertEqual(res["answered"], ["Q001"])
        self.assertEqual(res["by_file"], {"docs/Answers.md": ["Q001", "Q002 (open)"]})
        self.assertEqual(self.store()[0]["answer"], "Postgres")


class TestReviewFindings(Base):
    """The 0.7.0 review: no refusals of ordinary work, no opt-in by accident, worktrees are not strays."""

    def guard(self, path, content):
        return self.hook(bq.hook_guard_write, {"cwd": str(self.root), "tool_name": "Write",
                                               "tool_input": {"file_path": str(self.root / path), "content": content}})

    def test_ordinary_writes_are_not_refused(self):
        self.enroll()
        self.assertEqual(self.guard("HANDOFF.md", "- Decision: keep it (undo: revert PR #9)\nDecided without you: x"), "")
        self.assertEqual(self.guard("docs/plan.md", "## Assumptions\n\nA) do this\nB) or that\n"), "")
        self.assertEqual(self.guard("apps/CLAUDE.md", "## Open questions\n- see Q012"), "")
        self.assertEqual(self.guard("docs/decisions/0001-db.md", "Decision: Postgres"), "")
        self.assertEqual(self.guard(".github/ISSUE_TEMPLATE/question.yml", "name: Question"), "")
        self.assertEqual(self.guard("src/madrid.json", "{}"), "")
        self.assertEqual(self.guard("src/quadrant.json", "{}"), "")
        self.assertTrue(self.guard("docs/open-questions.md", "notes"))  # a new question file is still refused

    def test_lines_the_file_already_had_are_not_held_against_a_write(self):
        self.enroll()
        (self.root / "NOTES.md").write_text("# Notes\n\nQ1: Which DB?\n")
        self.assertEqual(self.guard("NOTES.md", "# Notes\n\nQ1: Which DB?\n\nShipped the thing.\n"), "")
        self.assertTrue(self.guard("NOTES.md", "# Notes\n\nQ1: Which DB?\nQ2: Dark mode?\n"))

    def test_a_list_started_by_a_question_does_not_switch_on_refusals_or_stop(self):
        self.hook(bq.hook_capture, self.ask_payload())
        self.assertTrue(bq.enrolled(self.root))
        self.assertFalse(bq.fully_enrolled(self.root))
        self.assertEqual(self.guard("docs/plan.md", "## Open questions\n- Which DB?"), "")
        self.assertEqual(self.hook(bq.hook_stop, {"cwd": str(self.root)}), "")

    def test_capture_in_a_folder_that_cannot_be_written_keeps_the_question(self):
        real_mkdir = bq.Path.mkdir
        root = str(self.root)

        def mkdir(path, *a, **kw):
            if str(path).startswith(root):
                raise PermissionError("read-only checkout")
            return real_mkdir(path, *a, **kw)
        with mock.patch.object(bq.Path, "mkdir", mkdir):
            out = self.hook(bq.hook_capture, self.ask_payload())
        self.assertIn("on this computer", out)
        self.assertEqual(len(bq.read_jsonl(bq.log_path("acme/app"))), 2)

    def test_worktrees_and_ignored_files_are_not_strays(self):
        self.enroll()
        (self.root / ".gitignore").write_text(".claude/worktrees/\n")
        wt = self.root / ".claude" / "worktrees" / "lane1"
        (wt / ".claude" / "build-questions").mkdir(parents=True)
        (wt / ".claude" / "build-questions" / "QUESTIONS.md").write_text("## Open questions\n")
        (wt / "Answers.md").write_text("Q1: x\n")
        (self.root / "Answers.md").write_text("Q1: x\n")
        (self.root / ".claude" / "plans").mkdir(parents=True)
        (self.root / ".claude" / "plans" / "x.md").write_text("## Open questions\n- Which DB?\n")
        self.assertEqual(sorted(f["path"] for f in bq.scan_repo(self.root)), [".claude/plans/x.md", "Answers.md"])

    def test_plugin_says_when_the_repo_copy_is_older(self):
        self.enroll()
        tool = self.root / bq.TOOL_REL / "bq.py"
        tool.write_text(tool.read_text().replace(f'VERSION = "{bq.VERSION}"', 'VERSION = "0.6.9"'))
        out = self.hook(bq.hook_session_start, {"cwd": str(self.root)}, as_repo_copy=False)
        self.assertIn("0.6.9", out)

    def test_settings_entry_shared_with_the_users_own_hook_keeps_theirs(self):
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "echo mine"},
            {"type": "command", "command": bq.repo_hook_command("hook-stop")}]}], "Weird": "x"}}))
        self.enroll()
        cfg = json.loads((self.root / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for e in cfg["hooks"]["Stop"] for h in e["hooks"]]
        self.assertEqual(cmds.count("echo mine"), 1)
        self.assertEqual(sum(bq.HOOK_MARK in c for c in cmds), 1)
        (self.root / ".claude" / "settings.json").write_text(json.dumps({"hooks": "not a dict"}))
        self.assertEqual(json.loads(run(["hooks", "--root", str(self.root)]))["repo_hooks"], "updated")


class TestScanImport(Base):
    def test_scan_finds_strays_and_import_marks_them(self):
        self.enroll()
        (self.root / "docs").mkdir()
        (self.root / "docs" / "plan.md").write_text("# Plan\n\n## Open questions\n\n- [ ] Do we bill monthly?\n")
        (self.root / "DECISIONS.md").write_text("Decision: use Stripe\n")
        (self.root / "README.md").write_text("# App\nNothing here.\n")
        found = {s["path"] for s in json.loads(run(["scan", "--root", str(self.root)]))}
        self.assertEqual(found, {"docs/plan.md", "DECISIONS.md"})  # CLAUDE.md's managed block is ignored
        cands = [{"question": "Do we bill monthly?", "answer": "", "topic": "Billing",
                  "source_ref": {"type": "file", "path": "docs/plan.md", "line": 5}},
                 {"question": "Which payment provider?", "answer": "Stripe", "source": "decided",
                  "source_ref": {"type": "file", "path": "DECISIONS.md", "line": 1}}]
        f = Path(self.tmp.name) / "c.json"
        f.write_text(json.dumps(cands))
        res = json.loads(run(["import", "--root", str(self.root), "--file", str(f)]))
        self.assertEqual(res["added"], 2)
        self.assertEqual(json.loads(run(["scan", "--root", str(self.root)])), [])
        res = json.loads(run(["import", "--root", str(self.root), "--file", str(f)]))
        self.assertEqual(res["added"], 0)

    def test_scan_mark(self):
        self.enroll()
        (self.root / "faq.md").write_text("Q: what?\n")
        run(["scan", "--root", str(self.root), "--mark", "faq.md"])
        self.assertEqual(json.loads(run(["scan", "--root", str(self.root)])), [])


class TestHooks(Base):
    def test_first_capture_starts_the_list_inside_the_repo(self):
        out = self.hook(bq.hook_capture, self.ask_payload())
        self.assertIn("questions.jsonl", out)
        self.assertEqual(len(self.store()), 2)                       # in the repo, not on this computer
        self.assertFalse(bq.log_path("acme/app").exists())
        self.assertTrue((bq.store_dir(self.root) / "README.md").exists())
        self.hook(bq.hook_capture, self.ask_payload(tool_use_id="toolu_10"))
        self.assertEqual(len(self.store()), 2)  # same questions again: merged, not duplicated

    def test_capture_outside_a_git_repo_waits_on_this_computer_then_merges(self):
        loose = Path(self.tmp.name) / "loose"
        loose.mkdir()
        out = self.hook(bq.hook_capture, self.ask_payload(cwd=str(loose)))
        self.assertIn("not a git repo", out)
        self.enroll()
        logged = bq.read_jsonl(bq.log_path(bq.detect_repo(str(loose))))
        self.assertEqual(len(logged), 2)

    def test_capture_ignores_other_tools_and_bad_input(self):
        self.assertEqual(self.hook(bq.hook_capture, {"tool_name": "Bash"}), "")
        with mock.patch("sys.stdin", io.StringIO("not json")):
            self.assertEqual(bq.hook_capture(None), 0)

    def test_stray_write(self):
        self.enroll()
        p = {"tool_name": "Write", "cwd": str(self.root),
             "tool_input": {"file_path": str(self.root / "docs" / "spec.md"), "content": "# Spec\n## Open questions\n- x?"}}
        self.assertIn("belong only in", self.hook(bq.hook_stray_write, p))
        p["tool_input"]["content"] = "# Spec\nJust prose."
        self.assertEqual(self.hook(bq.hook_stray_write, p), "")
        p["tool_input"] = {"file_path": str(bq.store_dir(self.root) / "README.md"), "content": "## Questions"}
        self.assertEqual(self.hook(bq.hook_stray_write, p), "")

    def test_stop_reminds_once_to_commit(self):
        self.enroll()
        first = self.hook(bq.hook_stop, {"cwd": str(self.root)})
        self.assertIn("commit", json.loads(first)["reason"])
        self.assertEqual(self.hook(bq.hook_stop, {"cwd": str(self.root)}), "")
        self.assertEqual(self.hook(bq.hook_stop, {"cwd": str(self.root), "stop_hook_active": True}), "")

    def test_session_start_mentions_strays(self):
        self.enroll()
        (self.root / "QUESTIONS.md").write_text("Q: stray?\n")
        ctx = json.loads(self.hook(bq.hook_session_start, {"cwd": str(self.root)}))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("QUESTIONS.md", ctx)
        self.assertIn("organize", ctx)


class TestPage(Base):
    def test_link_is_committed_so_other_accounts_find_the_page(self):
        self.enroll()
        run(["link", "https://claude.ai/artifact/abc", "--title", "App", "--root", str(self.root)])
        self.assertIn("claude.ai/artifact/abc", (self.root / ".claude" / "build-questions" / "page.json").read_text(encoding="utf-8"))
        self.assertIn("claude.ai/artifact/abc", (self.root / "CLAUDE.md").read_text(encoding="utf-8"))
        (self.home / "config.json").unlink()  # another account or computer: no local config
        self.assertEqual(json.loads(run(["status", "--root", str(self.root)]))["page"], "https://claude.ai/artifact/abc")
        ctx = json.loads(self.hook(bq.hook_session_start, {"cwd": str(self.root)}))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("claude.ai/artifact/abc", ctx)
        run(["link", "https://claude.ai/artifact/copy", "--account", "b@x.com", "--root", str(self.root)])
        st = json.loads(run(["status", "--root", str(self.root), "--account", "b@x.com"]))
        self.assertEqual((st["page"], st["main_page"]), ("https://claude.ai/artifact/copy", "https://claude.ai/artifact/abc"))
        (self.root / ".claude" / "build-questions" / "page.json").unlink()  # linked before page.json existed
        run(["link", "https://claude.ai/artifact/abc", "--repo", "acme/app", "--root", "/nonexistent"])  # config only
        self.hook(bq.hook_session_start, {"cwd": str(self.root)})
        self.assertTrue((self.root / ".claude" / "build-questions" / "page.json").exists())
        run(["unlink", "--root", str(self.root)])
        self.assertIsNone(json.loads(run(["status", "--root", str(self.root)]))["page"])

    def test_page_html_has_the_whole_list_built_in(self):
        self.enroll()
        run(["enroll", "--root", str(self.root), "--title", "Tidy <Haus>"])
        run(["record", "--root", str(self.root), "--question", "Closes </script><b>?", "--answer", "Green"])
        html = (self.root / ".claude" / "build-questions" / "page.html").read_text(encoding="utf-8")
        self.assertIn("<title>Tidy &lt;Haus&gt; Build Questions</title>", html)
        seed = html.split('id="bq-seed">\n', 1)[1].split("\n</script>", 1)[0]
        self.assertNotIn("<", seed)  # nothing in a question can close the script tag
        data = json.loads(seed)
        self.assertEqual([q["answer"] for q in data["questions"]], ["Green"])
        self.assertEqual(data["meta"]["count"], 1)
        st = json.loads(run(["status", "--root", str(self.root)]))
        self.assertTrue(st["page_html"].endswith("page.html"))
        self.assertIn("db", st["capabilities"])

    def test_transcript(self):
        t = Path(self.tmp.name) / "s.jsonl"
        lines = [
            {"type": "assistant", "sessionId": "s", "timestamp": "2026-10-01T10:00:00.000Z",
             "message": {"content": [{"type": "tool_use", "id": "toolu_A", "name": "AskUserQuestion", "input": ASK_INPUT}]}},
            {"type": "user", "toolUseResult": {"answers": {"Which database should the app use?": "Postgres (Recommended)"}},
             "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_A", "content": "ok"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_B", "name": "AskUserQuestion", "input": ASK_INPUT}]}},
        ]
        t.write_text("\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
        recs = bq.records_from_transcript(t, "o/r")
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["answer"], "Postgres (Recommended)")
        live = bq.records_from_ask(ASK_INPUT, {}, repo="o/r", tool_use_id="toolu_A")
        self.assertEqual(recs[0]["id"], live[0]["id"])


class TestDigest(Base):
    def test_digest_sections(self):
        self.enroll()
        recs = [bq.clean({"question": "Which database?", "answer": "SQLite", "asked_at": "2026-10-01T00:00:00Z", "n": 1}, "acme/app"),
                bq.clean({"question": "Monthly or yearly billing?", "answer": "", "asked_at": "2026-10-20T00:00:00Z", "n": 2}, "acme/app"),
                bq.clean({"question": "Button colour?", "answer": "Orange", "asked_at": "2026-09-01T00:00:00Z", "n": 3,
                          "updated_at": "2026-10-21T00:00:00Z", "history": [{"answer": "Green"}]}, "acme/app")]
        s, b = bq.compose_digest(recs, "acme/app", "https://claude.ai/artifact/x", "2026-10-15T00:00:00Z",
                                 [{"question_id": "q-1", "question": "Which database?", "status": "open", "proposed": "Postgres"}],
                                 {"added": 2, "merged": 1, "files": 1})
        self.assertIn("1 new, 1 changed, 1 change request, 1 waiting", s)
        for part in ("CHANGE REQUESTS", "WAITING FOR AN ANSWER", "NEW", "CHANGED", "was: Green", "Organized: 2"):
            self.assertIn(part, b)
        s, b = bq.compose_digest(recs[:1], "acme/app", "", "2026-10-15T00:00:00Z")
        self.assertIn("nothing new", s)

    def test_schedule_config(self):
        run(["schedule", "set", "--cron", "40 8 * * 1", "--tz", "America/Toronto", "--label", "Mondays 8:40",
             "--steps", "organize,sync,check,digest", "--where", "cloud"])
        self.assertEqual(bq.load_config()["schedule"]["cron"], "40 8 * * 1")
        run(["schedule", "clear"])
        self.assertNotIn("schedule", bq.load_config())


class TestEmail(unittest.TestCase):
    def test_compose(self):
        s, b = bq.compose_email([{"question_id": "q-1", "question": "DB?", "current_answer": "SQLite",
                                  "proposed": "Postgres", "reason": "concurrency"}], "o/r", "https://claude.ai/artifact/x")
        self.assertIn("1 change request ", s)
        self.assertIn("https://claude.ai/artifact/x#open", b)

    def test_bq_has_no_network_or_credentials(self):
        src = Path(bq.__file__).read_text(encoding="utf-8")
        for bad in ("urllib", "smtplib", "API_KEY", "PASSWORD", "import requests", "http.client"):
            self.assertNotIn(bad, src)


class TestMailServer(unittest.TestCase):
    def setUp(self):
        import mail_server
        self.m = mail_server

    def call(self, name, args=None):
        return self.m.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}})

    def test_protocol(self):
        init = self.m.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "build-questions-mail")
        self.assertIsNone(self.m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in self.m.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["email_status", "send_email"])

    def test_status_and_refusals_without_options(self):
        opts = {"BQ_EMAIL": "a@b.c", "BQ_METHOD": "resend"}
        with mock.patch.object(self.m, "opt", lambda name: opts.get(name, "")):
            st = json.loads(self.call("email_status")["result"]["content"][0]["text"])
            self.assertEqual(st["missing_options"], ["resend_api_key"])
            self.assertNotIn("resend_key", st)
            r = self.call("send_email", {"subject": "s", "body": "b"})["result"]
            self.assertTrue(r["isError"])
            self.assertIn("resend_api_key", r["content"][0]["text"])
        with mock.patch.object(self.m, "opt", lambda name: {"BQ_METHOD": "none"}.get(name, "")):
            self.assertIn("nothing is sent", self.call("send_email", {"subject": "s", "body": "b"})["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
