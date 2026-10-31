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
                        "https://github.com/acme/app.git"], check=True)
        self.env = mock.patch.dict(os.environ, {"BUILD_QUESTIONS_HOME": str(self.home), "HOME": str(t / "user")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def enroll(self):
        return json.loads(run(["enroll", "--root", str(self.root)]))

    def hook(self, fn, payload):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out):
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
            "https://github.com/shorconsulting/marketplace.git": "shorconsulting/marketplace",
            "git@github.com:ShorConsultingInc/tidyhaus.git": "ShorConsultingInc/tidyhaus",
            "http://local_proxy@127.0.0.1:1234/git/owner/repo": "owner/repo",
            "ssh://git@github.com/a/b/": "a/b",
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
        self.assertIn("merge=union", (self.root / ".gitattributes").read_text())
        res2 = self.enroll()
        self.assertEqual(res2["claude_md"], "unchanged")
        self.assertFalse(res2["gitattributes_changed"])
        self.assertEqual((self.root / "CLAUDE.md").read_text().count(bq.MEM_START), 1)

    def test_memory_block_replaced_in_place(self):
        (self.root / "CLAUDE.md").write_text("# App\n\nOwn notes.\n\n" + bq.MEM_START + "\nold\n" + bq.MEM_END + "\n\nMore.\n")
        self.enroll()
        txt = (self.root / "CLAUDE.md").read_text()
        self.assertIn("Own notes.", txt)
        self.assertIn("More.", txt)
        self.assertNotIn("\nold\n", txt)
        self.assertIn("questions.jsonl", txt)

    def test_user_memory(self):
        res = json.loads(run(["memory", "--user", "--root", str(self.root)]))
        self.assertEqual(res, {"user": "created"})
        self.assertIn("build-questions", (Path(os.environ["HOME"]) / ".claude" / "CLAUDE.md").read_text())

    def test_hand_appended_line_without_id_is_read(self):
        self.enroll()
        with bq.store_file(self.root).open("a") as f:
            f.write(json.dumps({"question": "Do we need dark mode?", "answer": "Yes"}) + "\n")
        recs = self.store()
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["id"].startswith("q-"))
        self.assertEqual(recs[0]["n"], 1)

    def test_union_merge_duplicates_latest_wins(self):
        self.enroll()
        base = {"id": "q-1", "question": "Which database?", "answer": "SQLite", "asked_at": "2026-10-01T00:00:00Z", "n": 1}
        newer = dict(base, answer="Postgres", updated_at="2026-10-03T00:00:00Z")
        bq.store_file(self.root).write_text(json.dumps(newer) + "\n" + json.dumps(base) + "\n")
        self.assertEqual(self.store()[0]["answer"], "Postgres")

    def test_record_and_set_answer(self):
        self.enroll()
        out = json.loads(run(["record", "--root", str(self.root), "--question", "Button colour?", "--answer", "Green",
                              "--topic", "Design"]))
        self.assertEqual(out["added"], 1)
        run(["set-answer", "--root", str(self.root), "Q001", "Orange", "--request", "r1"])
        r = self.store()[0]
        self.assertEqual((r["answer"], r["status"], r["history"][0]["answer"]), ("Orange", "changed", "Green"))
        self.assertIn("Orange", (bq.store_dir(self.root) / "QUESTIONS.md").read_text())


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
    def test_capture_goes_to_local_log_until_enrolled_then_merges(self):
        out = self.hook(bq.hook_capture, self.ask_payload())
        self.assertIn("locally", out)
        self.assertEqual(len(bq.read_jsonl(bq.log_path("acme/app"))), 2)
        self.enroll()
        res = json.loads(run(["merge-local", "--root", str(self.root)]))
        self.assertEqual(res["added"], 2)
        self.assertEqual(bq.read_jsonl(bq.log_path("acme/app")), [])
        out = self.hook(bq.hook_capture, self.ask_payload(tool_use_id="toolu_10"))
        self.assertIn("questions.jsonl", out)
        self.assertEqual(len(self.store()), 2)  # same questions again: merged, not duplicated

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
    def test_diff_and_pull(self):
        self.enroll()
        run(["record", "--root", str(self.root), "--question", "Button colour?", "--answer", "Green"])
        rec = self.store()[0]
        writes = json.loads(run(["diff", "--root", str(self.root)]))
        self.assertEqual([(w["op"], w["doc_id"]) for w in writes], [("set", rec["id"])])
        pd = Path(self.tmp.name) / "page" / "questions"
        pd.mkdir(parents=True)
        (pd / f"{rec['id']}.json").write_text(json.dumps({"id": rec["id"], "version": 4, "data": bq.page_doc(rec)}))
        self.assertEqual(json.loads(run(["diff", "--root", str(self.root), "--page-dir", str(pd.parent)])), [])
        changed = dict(bq.page_doc(rec), answer="Orange", status="changed", updated_at="2099-01-01T00:00:00Z",
                       history=[{"answer": "Green", "at": "2099-01-01T00:00:00Z"}])
        (pd / f"{rec['id']}.json").write_text(json.dumps({"id": rec["id"], "version": 5, "data": changed}))
        res = json.loads(run(["pull", "--root", str(self.root), "--page-dir", str(pd.parent)]))
        self.assertEqual(res["updated"], 1)
        self.assertEqual(self.store()[0]["answer"], "Orange")


class TestBackfill(Base):
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

    def test_missing_settings(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            for m, to in (("resend", "a@b.c"), ("smtp", "a@b.c"), ("resend", "")):
                with self.assertRaises(SystemExit):
                    bq.send_email(m, to, "s", "b")


if __name__ == "__main__":
    unittest.main()
