import io
import json
import os
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


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BUILD_QUESTIONS_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_hook(self, fn, payload):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out):
            fn(None)
        return out.getvalue()


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


class TestParsing(Base):
    def test_answers_in_response_dict(self):
        resp = {"questions": ASK_INPUT["questions"],
                "answers": {"Which database should the app use?": "Postgres (Recommended)",
                            "Which sign-in methods?": "Email link, Google"},
                "annotations": {"Which database should the app use?": {"notes": "Neon is fine"}}}
        recs = bq.records_from_ask(ASK_INPUT, resp, repo="o/r", tool_use_id="toolu_1")
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["answer"], "Postgres (Recommended)")
        self.assertEqual(recs[0]["notes"], "Neon is fine")
        self.assertFalse(recs[0]["custom_answer"])
        self.assertEqual(recs[1]["answer"], "Email link, Google")
        self.assertFalse(recs[1]["custom_answer"])
        self.assertTrue(recs[1]["multi"])
        self.assertEqual(recs[0]["topic"], "Database")

    def test_answers_in_string_response(self):
        resp = 'User has answered your questions: "Which database should the app use?"="Something else: Mongo". ' \
               'You can now continue.'
        recs = bq.records_from_ask(ASK_INPUT, resp, repo="o/r", tool_use_id="toolu_2")
        self.assertEqual(recs[0]["answer"], "Something else: Mongo")
        self.assertTrue(recs[0]["custom_answer"])
        self.assertEqual(recs[1]["status"], "unanswered")

    def test_answers_in_tool_input(self):
        inp = dict(ASK_INPUT, answers={"Which sign-in methods?": "Google"})
        recs = bq.records_from_ask(inp, None, repo="o/r", tool_use_id="t")
        self.assertEqual(recs[1]["answer"], "Google")

    def test_ids_stable_per_tool_use(self):
        a = bq.records_from_ask(ASK_INPUT, {}, repo="o/r", tool_use_id="x", asked_at="2026-01-01T00:00:00Z")
        b = bq.records_from_ask(ASK_INPUT, {}, repo="o/r", tool_use_id="x", asked_at="2026-02-01T00:00:00Z")
        self.assertEqual([r["id"] for r in a], [r["id"] for r in b])
        self.assertNotEqual(a[0]["id"], a[1]["id"])


class TestStore(Base):
    def test_append_dedupes_and_pending(self):
        recs = bq.records_from_ask(ASK_INPUT, {}, repo="o/r", tool_use_id="x")
        self.assertEqual(len(bq.append_records("o/r", recs)), 2)
        self.assertEqual(len(bq.append_records("o/r", recs)), 0)
        self.assertEqual(len(bq.pending("o/r")), 2)
        bq.mark_synced("o/r", [recs[0]["id"]])
        self.assertEqual([r["id"] for r in bq.pending("o/r")], [recs[1]["id"]])

    def test_pending_batch_numbers_whole_log(self):
        recs = bq.records_from_ask(ASK_INPUT, {}, repo="o/r", tool_use_id="x")
        bq.append_records("o/r", recs)
        bq.mark_synced("o/r", [recs[0]["id"]])
        out = io.StringIO()
        with redirect_stdout(out):
            bq.main(["pending", "--repo", "o/r", "--batch"])
        batch = json.loads(out.getvalue())
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["collection"], "questions")
        self.assertEqual(batch[0]["data"]["n"], 2)


class TestHooks(Base):
    def payload(self, **kw):
        p = {"tool_name": "AskUserQuestion", "tool_input": ASK_INPUT, "session_id": "s1",
             "tool_use_id": "toolu_9", "cwd": self.tmp.name,
             "tool_response": {"answers": {"Which database should the app use?": "SQLite"}}}
        p.update(kw)
        return p

    def test_capture_records_and_reports(self):
        with mock.patch.object(bq, "detect_repo", return_value="o/r"):
            out = self.run_hook(bq.hook_capture, self.payload())
        self.assertIn("recorded 2 question", out)
        self.assertIn("setup", out)
        self.assertEqual(bq.read_records("o/r")[0]["answer"], "SQLite")

    def test_capture_ignores_other_tools_and_bad_input(self):
        out = self.run_hook(bq.hook_capture, {"tool_name": "Bash"})
        self.assertEqual(out, "")
        with mock.patch("sys.stdin", io.StringIO("not json")):
            self.assertEqual(bq.hook_capture(None), 0)

    def test_stop_blocks_once_for_linked_repo(self):
        bq.save_config({"projects": {"o/r": {"artifact": "https://claude.ai/artifact/x"}}})
        with mock.patch.object(bq, "detect_repo", return_value="o/r"):
            self.run_hook(bq.hook_capture, self.payload())
            first = self.run_hook(bq.hook_stop, {"cwd": self.tmp.name})
            second = self.run_hook(bq.hook_stop, {"cwd": self.tmp.name})
            active = self.run_hook(bq.hook_stop, {"cwd": self.tmp.name, "stop_hook_active": True})
        self.assertEqual(json.loads(first)["decision"], "block")
        self.assertEqual(second, "")
        self.assertEqual(active, "")

    def test_stop_silent_for_unlinked_repo(self):
        with mock.patch.object(bq, "detect_repo", return_value="o/r"):
            self.run_hook(bq.hook_capture, self.payload())
            self.assertEqual(self.run_hook(bq.hook_stop, {"cwd": self.tmp.name}), "")

    def test_session_start_mentions_page(self):
        bq.save_config({"projects": {"o/r": {"artifact": "https://claude.ai/artifact/x"}}})
        with mock.patch.object(bq, "detect_repo", return_value="o/r"):
            out = self.run_hook(bq.hook_session_start, {"cwd": self.tmp.name})
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("https://claude.ai/artifact/x", ctx)


class TestBackfill(Base):
    def test_transcript(self):
        t = Path(self.tmp.name) / "s.jsonl"
        lines = [
            {"type": "assistant", "sessionId": "s", "timestamp": "2026-10-01T10:00:00.000Z",
             "message": {"content": [{"type": "tool_use", "id": "toolu_A", "name": "AskUserQuestion",
                                      "input": ASK_INPUT}]}},
            {"type": "user", "timestamp": "2026-10-01T10:01:00.000Z",
             "toolUseResult": {"answers": {"Which database should the app use?": "Postgres (Recommended)"}},
             "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_A", "content": "ok"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_B",
                                                           "name": "AskUserQuestion", "input": ASK_INPUT}]}},
        ]
        t.write_text("\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
        recs = bq.records_from_transcript(t, "o/r")
        self.assertEqual(len(recs), 2)  # toolu_B never answered: skipped
        self.assertEqual(recs[0]["answer"], "Postgres (Recommended)")
        self.assertEqual(recs[0]["asked_at"], "2026-10-01T10:00:00.000Z")
        # same id as the live hook would give
        live = bq.records_from_ask(ASK_INPUT, {}, repo="o/r", tool_use_id="toolu_A")
        self.assertEqual(recs[0]["id"], live[0]["id"])


class TestEmail(unittest.TestCase):
    def test_compose(self):
        s, b = bq.compose_email([{"question_id": "q-1", "question": "DB?", "current_answer": "SQLite",
                                  "proposed": "Postgres", "reason": "We need concurrency"}],
                                "o/r", "https://claude.ai/artifact/x")
        self.assertIn("1 change request ", s)
        self.assertIn("Proposed: Postgres", b)
        self.assertIn("https://claude.ai/artifact/x#open", b)

    def test_missing_settings(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                bq.send_email("resend", "a@b.c", "s", "b")
            with self.assertRaises(SystemExit):
                bq.send_email("smtp", "a@b.c", "s", "b")
            with self.assertRaises(SystemExit):
                bq.send_email("resend", "", "s", "b")


if __name__ == "__main__":
    unittest.main()
