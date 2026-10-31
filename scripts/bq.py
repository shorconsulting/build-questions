#!/usr/bin/env python3
"""build-questions: record the questions Claude asks during a build, and the answers.

Stdlib only. Local store under $BUILD_QUESTIONS_HOME (default ~/.build-questions):

  config.json                 email, notify method, linked projects {owner/repo: {artifact, title}}
  log/<owner__repo>.jsonl     one JSON record per question (append-only; a later line with the same id wins)
  log/<owner__repo>.synced    ids already written to the project's page
  state/<owner__repo>.json    hook bookkeeping (last Stop reminder)

The page (a claude.ai artifact with a db) is the shared copy reviewers see. Hooks cannot reach the page, so
they write here and Claude copies pending records to the page (see docs/commands.md, "sync").

Hooks never fail the session: every hook entry point swallows errors and exits 0.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import smtplib
import subprocess
import sys
import urllib.request
from email.message import EmailMessage
from pathlib import Path

SCHEMA = 1


# ---------------------------------------------------------------- paths & config

def home() -> Path:
    return Path(os.environ.get("BUILD_QUESTIONS_HOME") or Path.home() / ".build-questions")


def load_config() -> dict:
    p = home() / "config.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    p = home() / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repo_from_remote(url: str) -> str | None:
    """owner/repo from any git remote URL (https, ssh, scp-style, local proxies)."""
    url = url.strip()
    if not url:
        return None
    url = re.sub(r"(\.git)?/*$", "", url)
    m = re.search(r"[:/]([^/:]+)/([^/:]+)$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def detect_repo(cwd: str | None = None) -> str:
    cwd = cwd or os.getcwd()
    try:
        out = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5)
        r = repo_from_remote(out.stdout) if out.returncode == 0 else None
        if r:
            return r
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if top.returncode == 0 and top.stdout.strip():
            return "local/" + Path(top.stdout.strip()).name
    except (OSError, subprocess.SubprocessError):
        pass
    return "local/" + Path(cwd).resolve().name


def key(repo: str) -> str:
    return repo.replace("/", "__")


def log_path(repo: str) -> Path:
    return home() / "log" / f"{key(repo)}.jsonl"


def synced_path(repo: str) -> Path:
    return home() / "log" / f"{key(repo)}.synced"


def state_path(repo: str) -> Path:
    return home() / "state" / f"{key(repo)}.json"


# ---------------------------------------------------------------- records

def make_id(*parts: str) -> str:
    h = hashlib.sha1("\x1f".join(p or "" for p in parts).encode()).hexdigest()[:8]
    return f"q-{h}"


def read_records(repo: str) -> list[dict]:
    """Records in first-seen order; a later line with the same id replaces the earlier one."""
    by_id: dict[str, dict] = {}
    try:
        with log_path(repo).open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("id"):
                    by_id[r["id"]] = r
    except OSError:
        pass
    return sorted(by_id.values(), key=lambda r: (r.get("asked_at") or "", r["id"]))


def append_records(repo: str, recs: list[dict]) -> list[dict]:
    """Append records whose id is new. Returns the ones actually written."""
    if not recs:
        return []
    seen = {r["id"] for r in read_records(repo)}
    new = [r for r in recs if r["id"] not in seen]
    if not new:
        return []
    p = log_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        for r in new:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    return new


def synced_ids(repo: str) -> set[str]:
    try:
        return {l.strip() for l in synced_path(repo).read_text().splitlines() if l.strip()}
    except OSError:
        return set()


def pending(repo: str) -> list[dict]:
    done = synced_ids(repo)
    return [r for r in read_records(repo) if r["id"] not in done]


def mark_synced(repo: str, ids: list[str]) -> int:
    done = synced_ids(repo)
    add = [i for i in ids if i not in done]
    if add:
        p = synced_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write("".join(i + "\n" for i in add))
    return len(add)


def number_records(recs: list[dict]) -> None:
    for n, r in enumerate(recs, 1):
        r["n"] = n


# ---------------------------------------------------------------- AskUserQuestion parsing

_PAIR = re.compile(r'"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"')


def _answers_from(obj) -> tuple[dict, dict]:
    """(answers, annotations) from a tool_input / tool_response / toolUseResult of any shape."""
    answers: dict = {}
    notes: dict = {}
    if isinstance(obj, dict):
        a = obj.get("answers")
        if isinstance(a, dict):
            answers.update({str(k): v for k, v in a.items()})
        n = obj.get("annotations")
        if isinstance(n, dict):
            notes.update({str(k): v for k, v in n.items()})
        for k in ("content", "result", "text"):
            if not answers and k in obj:
                a2, n2 = _answers_from(obj[k])
                answers.update(a2)
                notes.update(n2)
    elif isinstance(obj, list):
        for item in obj:
            a2, n2 = _answers_from(item)
            answers.update(a2)
            notes.update(n2)
    elif isinstance(obj, str):
        for q, a in _PAIR.findall(obj):
            answers[q.replace('\\"', '"')] = a.replace('\\"', '"')
    return answers, notes


def _fmt_answer(v) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return "" if v is None else str(v)


def records_from_ask(tool_input: dict, tool_response, *, repo: str, session: str = "",
                     tool_use_id: str = "", asked_at: str | None = None) -> list[dict]:
    questions = (tool_input or {}).get("questions") or []
    answers, notes = _answers_from(tool_response)
    a_in, n_in = _answers_from(tool_input)
    for k, v in a_in.items():
        answers.setdefault(k, v)
    for k, v in n_in.items():
        notes.setdefault(k, v)
    asked_at = asked_at or now_iso()
    out = []
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            continue
        text = str(q.get("question") or "").strip()
        if not text:
            continue
        opts = []
        for o in q.get("options") or []:
            if isinstance(o, dict):
                opts.append({"label": str(o.get("label") or ""), "description": str(o.get("description") or "")})
            else:
                opts.append({"label": str(o), "description": ""})
        ans = answers.get(text)
        note = notes.get(text)
        if isinstance(note, dict):
            note = note.get("notes") or ""
        labels = {o["label"] for o in opts}
        answer = _fmt_answer(ans)
        parts = [p.strip() for p in answer.split(",")] if answer else []
        out.append({
            "schema": SCHEMA,
            "id": make_id(repo, tool_use_id or session, text, "" if tool_use_id else asked_at, str(i)),
            "repo": repo,
            "asked_at": asked_at,
            "session": session,
            "source": "ask",
            "topic": str(q.get("header") or "").strip(),
            "question": text,
            "options": opts,
            "multi": bool(q.get("multiSelect")),
            "answer": answer,
            "custom_answer": bool(answer) and not all(p in labels for p in parts),
            "notes": str(note or ""),
            "status": "answered" if answer else "unanswered",
        })
    return out


# ---------------------------------------------------------------- hooks

def _hook_input() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}


def _emit(event: str, context: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}))


def hook_capture(_args) -> int:
    try:
        data = _hook_input()
        if data.get("tool_name") != "AskUserQuestion":
            return 0
        repo = detect_repo(data.get("cwd"))
        recs = records_from_ask(data.get("tool_input") or {}, data.get("tool_response"),
                                repo=repo, session=data.get("session_id", ""),
                                tool_use_id=data.get("tool_use_id", ""))
        new = append_records(repo, recs)
        if new:
            linked = repo in (load_config().get("projects") or {})
            ids = ", ".join(r["id"] for r in new)
            msg = f"build-questions recorded {len(new)} question(s) for {repo} ({ids})."
            msg += " They will be copied to the review page at the next sync." if linked else \
                " This repo has no review page yet; /build-questions:setup links one."
            _emit("PostToolUse", msg)
    except Exception:  # never break the session
        pass
    return 0


def hook_session_start(_args) -> int:
    try:
        data = _hook_input()
        repo = detect_repo(data.get("cwd"))
        cfg = load_config()
        proj = (cfg.get("projects") or {}).get(repo)
        n_pending = len(pending(repo))
        if proj:
            _emit("SessionStart",
                  f"build-questions is on for {repo}: questions you ask with AskUserQuestion are recorded and "
                  f"reviewers can request changes on the page {proj.get('artifact')}. {n_pending} recorded "
                  f"question(s) are not on the page yet. Before starting build work in this repo, follow the "
                  f"build-questions skill's 'Start of work' step (sync, then check for change requests).")
        elif n_pending:
            _emit("SessionStart",
                  f"build-questions has {n_pending} recorded question(s) for {repo} but no review page. "
                  f"Mention once, at a natural point, that /build-questions:setup links one.")
    except Exception:
        pass
    return 0


def hook_stop(_args) -> int:
    """Block one stop when a linked repo has questions not yet on the page (cloud containers are ephemeral)."""
    try:
        data = _hook_input()
        if data.get("stop_hook_active"):
            return 0
        repo = detect_repo(data.get("cwd"))
        proj = (load_config().get("projects") or {}).get(repo)
        if not proj:
            return 0
        ids = sorted(r["id"] for r in pending(repo))
        if not ids:
            return 0
        sig = hashlib.sha1(",".join(ids).encode()).hexdigest()
        sp = state_path(repo)
        try:
            st = json.loads(sp.read_text())
        except (OSError, ValueError):
            st = {}
        if st.get("last_stop_reminder") == sig:
            return 0
        st["last_stop_reminder"] = sig
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(st))
        print(json.dumps({"decision": "block", "reason":
              f"build-questions: {len(ids)} recorded question(s) for {repo} are not on the review page yet. "
              f"Copy them now (docs/commands.md 'sync' in the build-questions plugin: run "
              f"`bq.py pending`, write each to questions/<id> with ArtifactData, then `bq.py mark-synced`), "
              f"then finish your reply in one line."}))
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------- transcript backfill

def _iter_jsonl(p: Path):
    try:
        with p.open() as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def records_from_transcript(p: Path, repo: str) -> list[dict]:
    asks: dict[str, tuple[dict, str, str]] = {}
    results: dict[str, object] = {}
    for ev in _iter_jsonl(p):
        msg = ev.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use" and item.get("name") == "AskUserQuestion":
                asks[item.get("id", "")] = (item.get("input") or {}, ev.get("timestamp") or "", ev.get("sessionId") or "")
            elif item.get("type") == "tool_result" and item.get("tool_use_id") in asks:
                results[item["tool_use_id"]] = ev.get("toolUseResult") or item.get("content")
    out = []
    for tid, (inp, ts, sess) in asks.items():
        if tid not in results:
            continue
        ts = (ts or now_iso()).replace("+00:00", "Z")
        out.extend(records_from_ask(inp, results[tid], repo=repo, session=sess, tool_use_id=tid, asked_at=ts))
    return out


def default_transcript_dir(cwd: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
    return Path.home() / ".claude" / "projects" / slug


# ---------------------------------------------------------------- email

def compose_email(requests: list[dict], repo: str, page: str) -> tuple[str, str]:
    n = len(requests)
    subject = f"[{repo}] {n} change request{'s' if n != 1 else ''} on build questions"
    lines = [f"Reviewers asked to change {n} answer{'s' if n != 1 else ''} in {repo}.", ""]
    for r in requests:
        lines += [
            f"• {r.get('question_id', '')}: {r.get('question', '').strip()}",
            f"  Current answer: {r.get('current_answer') or '(none)'}",
            f"  Proposed: {r.get('proposed') or '(no specific answer)'}",
        ]
        if r.get("reason"):
            lines.append(f"  Why: {r['reason']}")
        if r.get("by_name"):
            lines.append(f"  From: {r['by_name']}")
        lines.append("")
    lines += [f"Review them on the page: {page}#changes",
              "Or, in Claude Code inside this repo: /build-questions:changes", "",
              "Sent by the build-questions plugin."]
    return subject, "\n".join(lines)


def send_email(method: str, to: str, subject: str, body: str, sender: str | None = None) -> str:
    if not to:
        raise SystemExit("no recipient: set one with `bq.py config set email you@example.com`")
    if method == "resend":
        api_key = os.environ.get("RESEND_API_KEY")
        if not api_key:
            raise SystemExit("RESEND_API_KEY is not set in this environment")
        payload = json.dumps({"from": sender or "Build Questions <onboarding@resend.dev>",
                              "to": [to], "subject": subject, "text": body}).encode()
        req = urllib.request.Request("https://api.resend.com/emails", data=payload, method="POST", headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            "User-Agent": "build-questions/0.1"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return "resend: " + json.loads(resp.read() or b"{}").get("id", "sent")
    if method == "smtp":
        host = os.environ.get("BQ_SMTP_HOST")
        if not host:
            raise SystemExit("BQ_SMTP_HOST is not set in this environment")
        port = int(os.environ.get("BQ_SMTP_PORT") or 587)
        user = os.environ.get("BQ_SMTP_USER")
        pw = os.environ.get("BQ_SMTP_PASSWORD")
        msg = EmailMessage()
        msg["From"] = sender or os.environ.get("BQ_SMTP_FROM") or user or to
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            s = smtplib.SMTP(host, port, timeout=20)
            s.starttls()
        with s:
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
        return f"smtp: sent via {host}"
    raise SystemExit(f"email method {method!r} is not sent by this script (see docs/commands.md 'check')")


# ---------------------------------------------------------------- CLI

def _repo(args) -> str:
    return getattr(args, "repo", None) or detect_repo()


def cmd_repo(args) -> int:
    print(_repo(args))
    return 0


def cmd_config(args) -> int:
    cfg = load_config()
    if args.action == "show":
        print(json.dumps(cfg, indent=2, sort_keys=True))
    elif args.action == "get":
        cur = cfg
        for part in args.key.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        print(json.dumps(cur) if not isinstance(cur, str) else cur)
    elif args.action == "set":
        cur = cfg
        parts = args.key.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = args.value
        save_config(cfg)
        print(f"{args.key} = {args.value}")
    return 0


def cmd_link(args) -> int:
    cfg = load_config()
    repo = _repo(args)
    proj = cfg.setdefault("projects", {}).setdefault(repo, {})
    proj["artifact"] = args.artifact
    if args.title:
        proj["title"] = args.title
    save_config(cfg)
    print(f"{repo} -> {args.artifact}")
    return 0


def cmd_unlink(args) -> int:
    cfg = load_config()
    repo = _repo(args)
    (cfg.get("projects") or {}).pop(repo, None)
    save_config(cfg)
    print(f"{repo} unlinked (its recorded questions stay in {log_path(repo)})")
    return 0


def cmd_status(args) -> int:
    repo = _repo(args)
    cfg = load_config()
    recs = read_records(repo)
    print(json.dumps({
        "repo": repo,
        "page": ((cfg.get("projects") or {}).get(repo) or {}).get("artifact"),
        "email": cfg.get("email"),
        "notify": cfg.get("notify"),
        "recorded": len(recs),
        "pending_sync": len(pending(repo)),
        "store": str(log_path(repo)),
    }, indent=2))
    return 0


def cmd_record(args) -> int:
    repo = _repo(args)
    asked = now_iso()
    opts = []
    if args.options:
        try:
            raw = json.loads(args.options)
        except ValueError:
            raw = [o.strip() for o in args.options.split("|") if o.strip()]
        for o in raw:
            opts.append(o if isinstance(o, dict) else {"label": str(o), "description": ""})
    rec = {
        "schema": SCHEMA,
        "id": make_id(repo, args.session or "", args.question, asked),
        "repo": repo, "asked_at": asked, "session": args.session or "",
        "source": args.source, "topic": args.topic or "", "question": args.question.strip(),
        "options": opts, "multi": False, "answer": args.answer or "",
        "custom_answer": bool(opts) and (args.answer or "") not in {o["label"] for o in opts},
        "notes": args.notes or "",
        "status": "answered" if args.answer else "unanswered",
    }
    append_records(repo, [rec])
    print(rec["id"])
    return 0


def _page_doc(r: dict) -> dict:
    d = {k: r.get(k) for k in ("id", "n", "repo", "asked_at", "source", "topic", "question", "options",
                               "multi", "answer", "custom_answer", "notes", "status")}
    d["recorded_by"] = "build-questions"
    return d


def cmd_pending(args) -> int:
    repo = _repo(args)
    allr = read_records(repo)
    number_records(allr)
    done = synced_ids(repo)
    docs = [_page_doc(r) for r in allr if r["id"] not in done]
    if args.batch:
        print(json.dumps([{"op": "set", "collection": "questions", "doc_id": d["id"], "data": d} for d in docs],
                         indent=1))
    else:
        print(json.dumps(docs, indent=1))
    return 0


def cmd_mark_synced(args) -> int:
    repo = _repo(args)
    ids = args.ids or ([r["id"] for r in pending(repo)] if args.all else [])
    print(f"{mark_synced(repo, ids)} marked synced for {repo}")
    return 0


def cmd_list(args) -> int:
    repo = _repo(args)
    recs = read_records(repo)
    number_records(recs)
    if args.format == "json":
        print(json.dumps(recs, indent=1))
        return 0
    print(f"# Build questions: {repo}\n")
    for r in recs:
        topic = f" · {r['topic']}" if r.get("topic") else ""
        print(f"**Q{r['n']:03d}**{topic} · {r['asked_at'][:10]} · `{r['id']}`  ")
        print(f"{r['question']}  ")
        print(f"→ **{r.get('answer') or '(unanswered)'}**" + (f"  \n  _{r['notes']}_" if r.get("notes") else ""))
        print()
    return 0


def cmd_backfill(args) -> int:
    repo = _repo(args)
    d = Path(args.transcripts) if args.transcripts else default_transcript_dir(args.cwd or os.getcwd())
    files = sorted(d.glob("*.jsonl")) if d.is_dir() else ([d] if d.is_file() else [])
    found = []
    for f in files:
        found.extend(records_from_transcript(f, repo))
    new = append_records(repo, found)
    print(json.dumps({"repo": repo, "transcripts": len(files), "found": len(found), "added": len(new),
                      "dir": str(d)}))
    return 0


def cmd_email(args) -> int:
    cfg = load_config()
    reqs = json.loads(Path(args.requests).read_text())
    if isinstance(reqs, dict):
        reqs = [reqs]
    repo = args.repo or detect_repo()
    page = args.page or ((cfg.get("projects") or {}).get(repo) or {}).get("artifact") or ""
    subject, body = compose_email(reqs, repo, page)
    if args.dry_run:
        print(json.dumps({"subject": subject, "body": body}, indent=1))
        return 0
    notify = cfg.get("notify") or {}
    method = args.method or notify.get("method") or "none"
    to = args.to or cfg.get("email") or ""
    print(send_email(method, to, subject, body, args.sender or notify.get("from")))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bq.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("hook-capture", hook_capture), ("hook-session-start", hook_session_start),
                     ("hook-stop", hook_stop)):
        sub.add_parser(name).set_defaults(fn=fn)

    p = sub.add_parser("repo", help="print the owner/repo this checkout records under")
    p.add_argument("--repo")
    p.set_defaults(fn=cmd_repo)

    p = sub.add_parser("config")
    p.add_argument("action", choices=["show", "get", "set"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("link", help="link this repo to its review page")
    p.add_argument("artifact")
    p.add_argument("--repo")
    p.add_argument("--title")
    p.set_defaults(fn=cmd_link)

    p = sub.add_parser("unlink")
    p.add_argument("--repo")
    p.set_defaults(fn=cmd_unlink)

    p = sub.add_parser("status")
    p.add_argument("--repo")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("record", help="record a question asked in plain text, or a decision made without asking")
    p.add_argument("--question", required=True)
    p.add_argument("--answer", default="")
    p.add_argument("--topic")
    p.add_argument("--options", help='JSON list, or "A|B|C"')
    p.add_argument("--notes")
    p.add_argument("--source", choices=["prose", "decided", "ask"], default="prose")
    p.add_argument("--session")
    p.add_argument("--repo")
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("pending", help="records not yet on the page, as page documents")
    p.add_argument("--repo")
    p.add_argument("--batch", action="store_true", help="print as an ArtifactData batch of set writes")
    p.set_defaults(fn=cmd_pending)

    p = sub.add_parser("mark-synced")
    p.add_argument("ids", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--repo")
    p.set_defaults(fn=cmd_mark_synced)

    p = sub.add_parser("list")
    p.add_argument("--repo")
    p.add_argument("--format", choices=["md", "json"], default="md")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("backfill", help="recover AskUserQuestion answers from this project's transcripts")
    p.add_argument("--transcripts", help="a transcript .jsonl or a folder of them")
    p.add_argument("--cwd")
    p.add_argument("--repo")
    p.set_defaults(fn=cmd_backfill)

    p = sub.add_parser("email", help="email the owner about change requests")
    p.add_argument("--requests", required=True, help="JSON file: one request or a list")
    p.add_argument("--repo")
    p.add_argument("--page")
    p.add_argument("--to")
    p.add_argument("--method", choices=["resend", "smtp"])
    p.add_argument("--sender")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_email)

    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
