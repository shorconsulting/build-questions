#!/usr/bin/env python3
"""build-questions: one central, committed list of every question asked while building an app.

Stdlib only.

THE CENTRAL LIST (source of truth, committed in each repo)
  <repo>/.claude/build-questions/questions.jsonl   one JSON object per line, one line per question
  <repo>/.claude/build-questions/QUESTIONS.md      readable copy, regenerated on every write (don't edit)
  <repo>/.claude/build-questions/README.md         the line format, for sessions without the plugin
  <repo>/.claude/build-questions/sources.json      files already merged in, with their content hash
  <repo>/.gitattributes                             "questions.jsonl merge=union" so branches merge cleanly
  <repo>/CLAUDE.md                                  managed block telling every session where questions go

  A line may repeat an id (union merges, appends without the plugin): the copy with the latest
  `updated_at` wins. Lines without an id get one from their question text.

LOCAL (per computer, under $BUILD_QUESTIONS_HOME, default ~/.build-questions)
  config.json                 email, notify method, linked projects {owner/repo: {artifact, title}}
  log/<owner__repo>.jsonl     questions captured in repos not enrolled yet (merged in by `organize`)
  state/<owner__repo>.json    hook bookkeeping

THE PAGE (claude.ai artifact) is the review surface. `diff` and `pull` reconcile it with the central list.

Hooks never fail the session: every hook entry point swallows errors and exits 0.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
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

SCHEMA = 2
STORE_REL = Path(".claude") / "build-questions"
SIMILAR = 0.9
MEM_START = "<!-- build-questions:start -->"
MEM_END = "<!-- build-questions:end -->"


# ---------------------------------------------------------------- paths & config

def home() -> Path:
    return Path(os.environ.get("BUILD_QUESTIONS_HOME") or Path.home() / ".build-questions")


def load_config() -> dict:
    try:
        return json.loads((home() / "config.json").read_text())
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


def _git(cwd, *args, timeout=5) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def repo_root(cwd: str | None = None) -> Path | None:
    top = _git(cwd or os.getcwd(), "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def detect_repo(cwd: str | None = None) -> str:
    cwd = cwd or os.getcwd()
    r = repo_from_remote(_git(cwd, "remote", "get-url", "origin") or "")
    if r:
        return r
    top = repo_root(cwd)
    return "local/" + (top.name if top else Path(cwd).resolve().name)


def key(repo: str) -> str:
    return repo.replace("/", "__")


def log_path(repo: str) -> Path:
    return home() / "log" / f"{key(repo)}.jsonl"


def state_path(repo: str) -> Path:
    return home() / "state" / f"{key(repo)}.json"


def store_dir(root: Path) -> Path:
    return Path(root) / STORE_REL


def store_file(root: Path) -> Path:
    return store_dir(root) / "questions.jsonl"


def enrolled(root: Path | None) -> bool:
    return bool(root) and store_file(root).exists()


def load_state(repo: str) -> dict:
    try:
        return json.loads(state_path(repo).read_text())
    except (OSError, ValueError):
        return {}


def save_state(repo: str, st: dict) -> None:
    p = state_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st))


# ---------------------------------------------------------------- records

def norm_q(text: str) -> str:
    t = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def make_id(*parts: str) -> str:
    h = hashlib.sha1("\x1f".join(p or "" for p in parts).encode()).hexdigest()[:8]
    return f"q-{h}"


def stamp(r: dict) -> str:
    return r.get("updated_at") or r.get("asked_at") or ""


def clean(r: dict, repo: str = "") -> dict | None:
    """Fill defaults on a record of any vintage (schema 1, hand-appended lines)."""
    if not isinstance(r, dict) or not str(r.get("question") or "").strip():
        return None
    r = dict(r)
    r["question"] = str(r["question"]).strip()
    r.setdefault("repo", repo)
    r["id"] = r.get("id") or make_id(repo, norm_q(r["question"]))
    r.setdefault("asked_at", now_iso())
    r.setdefault("updated_at", r["asked_at"])
    r.setdefault("source", "prose")
    r.setdefault("topic", "")
    r.setdefault("options", [])
    r.setdefault("multi", False)
    r["answer"] = "" if r.get("answer") is None else str(r.get("answer"))
    r.setdefault("notes", "")
    r.setdefault("status", "answered" if r["answer"] else "unanswered")
    if r["status"] == "unanswered" and r["answer"]:
        r["status"] = "answered"
    srcs = r.get("sources")
    if not isinstance(srcs, list) or not srcs:
        srcs = [{"type": r["source"]}]
    r["sources"] = srcs
    r.setdefault("aliases", [])
    r["schema"] = SCHEMA
    return r


def read_jsonl(path: Path, repo: str = "") -> list[dict]:
    """Records from a JSONL file; per id, the copy with the latest updated_at wins."""
    best: dict[str, dict] = {}
    order: list[str] = []
    try:
        with Path(path).open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = clean(json.loads(line), repo)
                except ValueError:
                    continue
                if not r:
                    continue
                cur = best.get(r["id"])
                if cur is None:
                    order.append(r["id"])
                if cur is None or stamp(r) >= stamp(cur):
                    best[r["id"]] = r
    except OSError:
        pass
    return [best[i] for i in order]


def sort_records(recs: list[dict]) -> list[dict]:
    return sorted(recs, key=lambda r: (r.get("n") or 10 ** 9, r.get("asked_at") or "", r["id"]))


def renumber(recs: list[dict]) -> list[dict]:
    """Keep each record's n; give records with no n, or a duplicate n (from a branch merge), the next free one."""
    recs = sorted(recs, key=lambda r: (r.get("asked_at") or "", r["id"]))
    used: set[int] = set()
    keep = []
    for r in sorted(recs, key=lambda r: (r.get("n") or 10 ** 9, r.get("asked_at") or "")):
        n = r.get("n")
        if isinstance(n, int) and n > 0 and n not in used:
            used.add(n)
        else:
            keep.append(r)
    nxt = max(used, default=0)
    for r in sorted(keep, key=lambda r: (r.get("asked_at") or "", r["id"])):
        nxt += 1
        r["n"] = nxt
    return sort_records(recs)


def _merge_sources(a: list, b: list) -> list:
    out, seen = [], set()
    for s in (a or []) + (b or []):
        k = json.dumps(s, sort_keys=True)
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out


def _find_similar(rec: dict, existing: list[dict]) -> dict | None:
    q = norm_q(rec["question"])
    best, score = None, 0.0
    for e in existing:
        s = difflib.SequenceMatcher(None, q, norm_q(e["question"])).ratio()
        if s > score:
            best, score = e, s
    return best if score >= SIMILAR else None


def merge_records(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], dict]:
    """Fold incoming records into existing ones: same id or near-identical question = same question.

    The newer answer wins and the older one moves to `history`; sources and ids are unioned; the earliest
    asked_at is kept. Returns (records, stats)."""
    recs = [dict(r) for r in existing]
    by_id = {}
    for r in recs:
        by_id[r["id"]] = r
        for a in r.get("aliases") or []:
            by_id[a] = r
    stats = {"added": 0, "merged": 0, "updated": 0}
    for inc in incoming:
        tgt = by_id.get(inc["id"]) or _find_similar(inc, recs)
        if tgt is None:
            new = dict(inc)
            new.pop("n", None) if any(r.get("n") == inc.get("n") for r in recs) else None
            recs.append(new)
            by_id[new["id"]] = new
            for a in new.get("aliases") or []:
                by_id[a] = new
            stats["added"] += 1
            continue
        if inc["id"] != tgt["id"]:
            tgt["aliases"] = sorted(set(tgt.get("aliases") or []) | {inc["id"]} | set(inc.get("aliases") or []))
            by_id[inc["id"]] = tgt
            stats["merged"] += 1
        tgt["sources"] = _merge_sources(tgt.get("sources"), inc.get("sources"))
        if inc.get("asked_at") and inc["asked_at"] < (tgt.get("asked_at") or "~"):
            tgt["asked_at"] = inc["asked_at"]
        for f in ("topic", "notes"):
            if not tgt.get(f) and inc.get(f):
                tgt[f] = inc[f]
        if not tgt.get("options") and inc.get("options"):
            tgt["options"] = inc["options"]
        newer = stamp(inc) > stamp(tgt)
        if inc.get("answer") and (not tgt.get("answer") or (newer and inc["answer"] != tgt["answer"])):
            if tgt.get("answer"):
                hist = list(tgt.get("history") or [])
                hist.append({"answer": tgt["answer"], "at": stamp(inc) or now_iso(),
                             **({"request_id": inc["request_id"]} if inc.get("request_id") else {})})
                tgt["history"] = hist
                tgt["status"] = "changed"
            else:
                tgt["status"] = "answered"
            tgt["answer"] = inc["answer"]
            tgt["updated_at"] = max(stamp(inc), stamp(tgt))
            stats["updated"] += 1
        if newer and inc.get("history") and len(inc["history"]) > len(tgt.get("history") or []):
            tgt["history"] = inc["history"]
            tgt["status"] = inc.get("status", tgt.get("status"))
            tgt["updated_at"] = stamp(inc)
    return renumber(recs), stats


# ---------------------------------------------------------------- the central list on disk

def read_store(root: Path, repo: str) -> list[dict]:
    return renumber(read_jsonl(store_file(root), repo))


def write_store(root: Path, recs: list[dict], repo: str) -> None:
    d = store_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    recs = renumber(recs)
    tmp = store_file(root).with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in recs))
    tmp.replace(store_file(root))
    (d / "QUESTIONS.md").write_text(render_md(recs, repo))


def add_to_store(root: Path, repo: str, incoming: list[dict]) -> dict:
    recs, stats = merge_records(read_store(root, repo), [clean(r, repo) for r in incoming if clean(r, repo)])
    write_store(root, recs, repo)
    return stats


def render_md(recs: list[dict], repo: str) -> str:
    cfg = load_config()
    page = ((cfg.get("projects") or {}).get(repo) or {}).get("artifact")
    out = [f"# Build questions: {repo}", "",
           "Generated from `questions.jsonl` by the build-questions plugin. Don't edit this file: record questions as",
           "described in `README.md` next to it." + (f" Review page: {page}" if page else ""), ""]
    open_ = [r for r in recs if not r.get("answer")]
    if open_:
        out += ["## Waiting for an answer", ""]
        out += [f"- **Q{r['n']:03d}**{' · ' + r['topic'] if r.get('topic') else ''}: {r['question']}" for r in open_]
        out.append("")
    topics: dict[str, list] = {}
    for r in recs:
        if r.get("answer"):
            topics.setdefault(r.get("topic") or "General", []).append(r)
    for t in sorted(topics):
        out += [f"## {t}", ""]
        for r in topics[t]:
            tag = " _(decided by Claude)_" if r.get("source") == "decided" else ""
            was = f" (was: ~~{r['history'][-1]['answer']}~~)" if r.get("history") else ""
            out.append(f"- **Q{r['n']:03d}** {r['question']}  \n  → **{r['answer']}**{was}{tag}"
                       + (f"  \n  _{r['notes']}_" if r.get("notes") else ""))
        out.append("")
    return "\n".join(out)


STORE_README = """# Build questions: the central list

Every product question asked while building this app, and its answer, lives in `questions.jsonl` here: one
JSON object per line. `QUESTIONS.md` is a readable copy, regenerated by the build-questions plugin.

## Adding a question without the plugin

Append one line (keep it on one line):

```json
{"question": "Which payment provider should take card payments?", "answer": "Stripe", "topic": "Payments", "source": "prose", "asked_at": "2026-10-31T17:00:00Z", "notes": "Why, if it helps a reviewer"}
```

- `question`: written so it makes sense on its own, months later.
- `answer`: empty string if not answered yet.
- `topic`: the area (Database, Auth, Pricing, Design, Scope...).
- `source`: `prose` (asked in chat), `decided` (Claude chose without asking), or `ask` (the question tool).
- Optional: `options` ([{"label", "description"}]), `notes`.

To change an answer, append the same question again with the new answer and a later `updated_at`: the plugin
keeps the old answer in `history`. The plugin fills in `id`, `n` and the rest the next time it runs.

## Don't keep questions anywhere else

No QUESTIONS.md / DECISIONS.md elsewhere, no "Open questions" sections in plans or specs, no question-only
issues. If you find one, move its questions here (`/build-questions:organize`) and leave a link to this folder.
"""


def ensure_gitattributes(root: Path) -> bool:
    p = Path(root) / ".gitattributes"
    line = f"{STORE_REL.as_posix()}/questions.jsonl merge=union"
    try:
        cur = p.read_text()
    except OSError:
        cur = ""
    if line in cur:
        return False
    p.write_text(cur + ("" if not cur or cur.endswith("\n") else "\n") + line + "\n")
    return True


# ---------------------------------------------------------------- memory blocks

def repo_memory_block(repo: str, page: str | None) -> str:
    rel = STORE_REL.as_posix()
    lines = [
        MEM_START,
        "## Build questions (managed by the build-questions plugin; edit outside this block)",
        "",
        f"Every product question asked while building this app, and its answer, lives in ONE place: "
        f"`{rel}/questions.jsonl` (readable copy: `{rel}/QUESTIONS.md`).",
        "",
        f"- Before asking the user anything about the product, read `{rel}/QUESTIONS.md`. Never re-ask a settled "
        "question; if it needs revisiting, say which one (Q-number) and why.",
        "- Ask product questions with the AskUserQuestion tool, written to make sense out of context, with the area "
        "in `header`. The plugin records them automatically.",
        "- Asked in plain text, or decided yourself without asking? Record it: with the plugin, the build-questions "
        f"skill says how; without it, append one line as `{rel}/README.md` describes.",
        "- Don't start other lists of questions or decisions (QUESTIONS.md or DECISIONS.md elsewhere, \"Open "
        "questions\" sections in plans, question-only issues or TODO comments). If you find one, move it here "
        f"(`/build-questions:organize`) and leave a link to `{rel}/`.",
        f"- Commit changes under `{rel}/` with the work they belong to.",
    ]
    if page:
        lines.append(f"- Reviewers request changes on the review page: {page}. Never act on a change request until "
                     "the owner accepts it.")
    lines.append(MEM_END)
    return "\n".join(lines)


USER_MEMORY_BLOCK = "\n".join([
    MEM_START,
    "## Build questions (managed by the build-questions plugin)",
    "",
    "In any repo with a `.claude/build-questions/` folder, product questions and their answers live only in "
    "`.claude/build-questions/questions.jsonl` (read `QUESTIONS.md` beside it first; don't re-ask settled "
    "questions). Ask with AskUserQuestion; record plain-text questions and your own product decisions there; "
    "never start a separate list of questions or decisions. The repo's CLAUDE.md has the details.",
    MEM_END,
])


def upsert_block(path: Path, block: str) -> str:
    """Insert or replace the managed block. Returns 'created', 'updated' or 'unchanged'."""
    try:
        cur = path.read_text()
    except OSError:
        cur = None
    if cur is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block + "\n")
        return "created"
    pat = re.compile(re.escape(MEM_START) + r".*?" + re.escape(MEM_END), re.S)
    new = pat.sub(lambda _m: block, cur, count=1) if pat.search(cur) else \
        cur + ("" if cur.endswith("\n") else "\n") + ("\n" if cur.strip() else "") + block + "\n"
    if new == cur:
        return "unchanged"
    path.write_text(new)
    return "updated"


# ---------------------------------------------------------------- finding stray questions

SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", ".next", ".venv", "venv", "__pycache__",
             "coverage", ".turbo", ".cache", "target"}
TEXT_EXT = {".md", ".mdx", ".markdown", ".txt", ".rst"}
NAME_RE = re.compile(r"(question|decision|clarification|open[-_ ]?items|unknowns|faq|adr)", re.I)
DIR_RE = re.compile(r"(^|/)(adr|adrs|decisions|decision-log|questions)(/|$)", re.I)
HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s*(open\s+|outstanding\s+|unresolved\s+|pending\s+)?"
                     r"(questions?|decisions?|clarifications?|unknowns|assumptions|to\s+confirm|tbd)\b", re.I)
LINE_RES = [
    re.compile(r"^\s*(\*\*)?Q\d*\s*[:.)]", re.I),
    re.compile(r"^\s*[-*]\s*\[ \]\s.*\?\s*$"),
    re.compile(r"^\s*(\*\*)?(decision|decided|answer)\s*[:：]", re.I),
]


def _sha(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def read_sources(root: Path) -> dict:
    try:
        return json.loads((store_dir(root) / "sources.json").read_text())
    except (OSError, ValueError):
        return {}


def write_sources(root: Path, src: dict) -> None:
    store_dir(root).mkdir(parents=True, exist_ok=True)
    (store_dir(root) / "sources.json").write_text(json.dumps(src, indent=1, sort_keys=True) + "\n")


def scan_file(path: Path, rel: str) -> list[dict]:
    hits = []
    try:
        if path.stat().st_size > 1_000_000:
            return []
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    in_block = False
    for i, line in enumerate(lines, 1):
        if line.strip().startswith(MEM_START):
            in_block = True
        if in_block:
            in_block = not line.strip().startswith(MEM_END)
            continue
        if HEAD_RE.match(line):
            hits.append({"line": i, "kind": "heading", "text": line.strip()[:160]})
        elif any(r.match(line) for r in LINE_RES):
            hits.append({"line": i, "kind": "item", "text": line.strip()[:160]})
    by_name = bool(NAME_RE.search(Path(rel).name) or DIR_RE.search(rel))
    if by_name and not hits:
        hits.append({"line": 1, "kind": "name", "text": "file name suggests questions or decisions"})
    return hits[:15]


def scan_repo(root: Path, include_known: bool = False, limit_files: int = 4000) -> list[dict]:
    root = Path(root)
    known = read_sources(root)
    out, seen = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and Path(os.path.normpath(os.path.join(rel_dir, d))).as_posix() != STORE_REL.as_posix()]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() not in TEXT_EXT:
                continue
            seen += 1
            if seen > limit_files:
                return out
            rel = p.relative_to(root).as_posix()
            hits = scan_file(p, rel)
            if not hits:
                continue
            sha = _sha(p)
            if not include_known and known.get(rel) == sha:
                continue
            out.append({"path": rel, "sha": sha, "previously_merged": rel in known, "hits": hits})
    return out


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
        note = notes.get(text)
        if isinstance(note, dict):
            note = note.get("notes") or ""
        labels = {o["label"] for o in opts}
        answer = _fmt_answer(answers.get(text))
        parts = [p.strip() for p in answer.split(",")] if answer else []
        out.append({
            "schema": SCHEMA,
            "id": make_id(repo, tool_use_id or session, text, "" if tool_use_id else asked_at, str(i)),
            "repo": repo, "asked_at": asked_at, "updated_at": asked_at, "session": session,
            "source": "ask", "sources": [{"type": "ask", "ref": tool_use_id or session}],
            "topic": str(q.get("header") or "").strip(), "question": text, "options": opts,
            "multi": bool(q.get("multiSelect")), "answer": answer,
            "custom_answer": bool(answer) and not all(p in labels for p in parts),
            "notes": str(note or ""), "status": "answered" if answer else "unanswered",
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
        cwd = data.get("cwd")
        repo = detect_repo(cwd)
        recs = records_from_ask(data.get("tool_input") or {}, data.get("tool_response"),
                                repo=repo, session=data.get("session_id", ""),
                                tool_use_id=data.get("tool_use_id", ""))
        if not recs:
            return 0
        root = repo_root(cwd)
        if enrolled(root):
            add_to_store(root, repo, recs)
            _emit("PostToolUse", f"build-questions recorded {len(recs)} question(s) in "
                                 f"{STORE_REL.as_posix()}/questions.jsonl. Commit it with this work.")
        else:
            p = log_path(repo)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as f:
                f.write("".join(json.dumps(r, sort_keys=True) + "\n" for r in recs))
            _emit("PostToolUse", f"build-questions saved {len(recs)} question(s) for {repo} locally; this repo "
                                 f"has no central list yet (/build-questions:setup creates it).")
    except Exception:  # never break the session
        pass
    return 0


def hook_stray_write(_args) -> int:
    """After Write/Edit: if the edit puts questions or decisions somewhere other than the central list, say so."""
    try:
        data = _hook_input()
        inp = data.get("tool_input") or {}
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        if not fp:
            return 0
        root = repo_root(data.get("cwd") or os.path.dirname(fp))
        if not enrolled(root):
            return 0
        try:
            rel = Path(fp).resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            return 0
        if rel.startswith(STORE_REL.as_posix()) or Path(rel).suffix.lower() not in TEXT_EXT:
            return 0
        text = "\n".join(str(inp.get(k) or "") for k in ("content", "new_string"))
        for e in inp.get("edits") or []:
            if isinstance(e, dict):
                text += "\n" + str(e.get("new_string") or "")
        hit = any(HEAD_RE.match(l) or any(r.match(l) for r in LINE_RES) for l in text.splitlines())
        if not hit and not (NAME_RE.search(Path(rel).name) or DIR_RE.search(rel)):
            return 0
        _emit("PostToolUse",
              f"build-questions: {rel} now holds questions or decisions. In this repo they belong only in "
              f"{STORE_REL.as_posix()}/questions.jsonl. Record each one there (the build-questions skill, "
              f"'Recording what the hook can't see'), then replace the list in {rel} with a link to "
              f"{STORE_REL.as_posix()}/QUESTIONS.md. Leave it if the file is not a list of questions.")
    except Exception:
        pass
    return 0


def hook_session_start(_args) -> int:
    try:
        data = _hook_input()
        cwd = data.get("cwd")
        repo = detect_repo(cwd)
        root = repo_root(cwd)
        cfg = load_config()
        proj = (cfg.get("projects") or {}).get(repo)
        local = read_jsonl(log_path(repo), repo)
        if enrolled(root):
            recs = read_store(root, repo)
            n_open = sum(1 for r in recs if not r.get("answer"))
            strays = scan_repo(root, limit_files=1500)
            msg = (f"build-questions: this repo's central list of product questions is "
                   f"{STORE_REL.as_posix()}/questions.jsonl ({len(recs)} questions, {n_open} unanswered; read "
                   f"QUESTIONS.md beside it before asking anything).")
            if proj:
                msg += f" Review page: {proj.get('artifact')}."
            todo = []
            if local:
                todo.append(f"{len(local)} question(s) captured before the list existed")
            if strays:
                todo.append(f"{len(strays)} file(s) with questions outside the list ("
                            + ", ".join(s["path"] for s in strays[:5]) + ("…" if len(strays) > 5 else "") + ")")
            if todo:
                msg += " To merge: " + "; ".join(todo) + ". Run the build-questions 'organize' steps before " \
                       "other work, and include the result in your next commit."
            if proj:
                msg += " Then follow the skill's 'Start of work' step (sync, check for change requests)."
            _emit("SessionStart", msg)
        elif local or proj:
            _emit("SessionStart",
                  f"build-questions has {len(local)} question(s) for {repo} saved on this computer but the repo has "
                  f"no central list. Mention once, at a natural point, that /build-questions:setup creates it.")
    except Exception:
        pass
    return 0


def _store_sig(root: Path) -> str:
    return _sha(store_file(root))


def hook_stop(_args) -> int:
    """Block one stop when the central list changed since the last sync, or isn't committed (cloud disks vanish)."""
    try:
        data = _hook_input()
        if data.get("stop_hook_active"):
            return 0
        cwd = data.get("cwd")
        root = repo_root(cwd)
        if not enrolled(root):
            return 0
        repo = detect_repo(cwd)
        proj = (load_config().get("projects") or {}).get(repo)
        st = load_state(repo)
        sig = _store_sig(root)
        dirty = bool(_git(root, "status", "--porcelain", "--", STORE_REL.as_posix()))
        need_sync = bool(proj) and st.get("synced_sig") != sig
        if not (need_sync or dirty):
            return 0
        remind = f"{sig}:{int(need_sync)}:{int(dirty)}"
        if st.get("last_stop_reminder") == remind:
            return 0
        st["last_stop_reminder"] = remind
        save_state(repo, st)
        steps = []
        if need_sync:
            steps.append("copy it to the review page (docs/commands.md 'sync')")
        if dirty:
            steps.append(f"commit {STORE_REL.as_posix()}/ with this work (or say why not)")
        print(json.dumps({"decision": "block", "reason":
              f"build-questions: the central question list changed. Before finishing: " + "; ".join(steps)
              + ". Then finish your reply in one line."}))
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
        for r in records_from_ask(inp, results[tid], repo=repo, session=sess, tool_use_id=tid, asked_at=ts):
            r["sources"] = [{"type": "transcript", "ref": f"{p.name}#{tid}"}]
            out.append(r)
    return out


def default_transcript_dir(cwd: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
    return Path.home() / ".claude" / "projects" / slug


# ---------------------------------------------------------------- the page

def read_page_dir(d: Path) -> dict[str, dict]:
    """questions/<id>.json files from `ArtifactData list ... out_dir`. Accepts bare docs or {id, data, version}."""
    out = {}
    qd = Path(d) / "questions" if (Path(d) / "questions").is_dir() else Path(d)
    for f in sorted(qd.glob("*.json")):
        try:
            obj = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        version = None
        if isinstance(obj, dict) and isinstance(obj.get("data"), dict):
            version = obj.get("version")
            doc_id = obj.get("id") or f.stem
            obj = obj["data"]
        else:
            doc_id = obj.get("id") or f.stem
            version = obj.pop("_version", None) if isinstance(obj, dict) else None
        if isinstance(obj, dict):
            obj["_doc_id"], obj["_version"] = doc_id, version
            out[doc_id] = obj
    return out


PAGE_FIELDS = ("id", "n", "repo", "asked_at", "updated_at", "source", "topic", "question", "options", "multi",
               "answer", "custom_answer", "notes", "status", "history", "aliases")


def page_doc(r: dict) -> dict:
    d = {k: r.get(k) for k in PAGE_FIELDS if r.get(k) is not None}
    d["recorded_by"] = "build-questions"
    return d


def _same(a: dict, b: dict) -> bool:
    return all((a.get(k) or None) == (b.get(k) or None) for k in PAGE_FIELDS)


def page_diff(recs: list[dict], page: dict[str, dict]) -> list[dict]:
    writes = []
    ids = {r["id"] for r in recs}
    for r in recs:
        cur = page.get(r["id"])
        doc = page_doc(r)
        if cur is None:
            writes.append({"op": "set", "collection": "questions", "doc_id": r["id"], "data": doc})
        elif not _same(doc, cur) and stamp(r) >= stamp(cur):
            w = {"op": "set", "collection": "questions", "doc_id": r["id"], "data": doc}
            if cur.get("_version"):
                w["if_version"] = cur["_version"]
            writes.append(w)
    # questions merged into another one: remove their old page copy
    aliased = {a for r in recs for a in (r.get("aliases") or [])}
    for doc_id, cur in page.items():
        if doc_id not in ids and doc_id in aliased:
            w = {"op": "delete", "collection": "questions", "doc_id": doc_id}
            if cur.get("_version"):
                w["if_version"] = cur["_version"]
            writes.append(w)
    return writes


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
    lines += [f"Review them on the page: {page}#open",
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
            "User-Agent": "build-questions/0.2"})
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
        s = smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)
        if port != 465:
            s.starttls()
        with s:
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
        return f"smtp: sent via {host}"
    raise SystemExit(f"email method {method!r} is not sent by this script (see docs/commands.md 'check')")


# ---------------------------------------------------------------- CLI

def _repo(args) -> str:
    return getattr(args, "repo", None) or detect_repo(getattr(args, "root", None))


def _root(args) -> Path:
    r = Path(args.root) if getattr(args, "root", None) else repo_root()
    if not r:
        raise SystemExit("not inside a git repository (pass --root)")
    return r


def _out(obj) -> None:
    print(json.dumps(obj, indent=1, ensure_ascii=False))


def cmd_repo(args) -> int:
    print(_repo(args))
    return 0


def cmd_config(args) -> int:
    cfg = load_config()
    if args.action == "show":
        _out(cfg)
    elif args.action == "get":
        cur = cfg
        for part in (args.key or "").split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        print(cur if isinstance(cur, str) else json.dumps(cur))
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
    print(f"{repo} unlinked")
    return 0


def cmd_enroll(args) -> int:
    """Create the central list, the merge rule and the repo's memory block. Idempotent."""
    root, repo = _root(args), _repo(args)
    d = store_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    created = not store_file(root).exists()
    if created:
        store_file(root).write_text("")
    (d / "README.md").write_text(STORE_README)
    recs = read_store(root, repo)
    write_store(root, recs, repo)
    ga = ensure_gitattributes(root)
    page = ((load_config().get("projects") or {}).get(repo) or {}).get("artifact")
    mem = upsert_block(root / "CLAUDE.md", repo_memory_block(repo, page))
    _out({"repo": repo, "store": str(store_file(root)), "created": created, "gitattributes_changed": ga,
          "claude_md": mem})
    return 0


def cmd_memory(args) -> int:
    res = {}
    if args.user:
        res["user"] = upsert_block(Path.home() / ".claude" / "CLAUDE.md", USER_MEMORY_BLOCK)
    if args.repo_memory or not args.user:
        root, repo = _root(args), _repo(args)
        page = ((load_config().get("projects") or {}).get(repo) or {}).get("artifact")
        res["repo"] = upsert_block(root / "CLAUDE.md", repo_memory_block(repo, page))
    _out(res)
    return 0


def cmd_status(args) -> int:
    repo = _repo(args)
    root = repo_root(getattr(args, "root", None))
    cfg = load_config()
    recs = read_store(root, repo) if enrolled(root) else []
    _out({
        "repo": repo, "root": str(root) if root else None, "enrolled": enrolled(root),
        "page": ((cfg.get("projects") or {}).get(repo) or {}).get("artifact"),
        "email": cfg.get("email"), "notify": cfg.get("notify"),
        "questions": len(recs), "unanswered": sum(1 for r in recs if not r.get("answer")),
        "captured_locally_not_merged": len(read_jsonl(log_path(repo), repo)),
        "store": str(store_file(root)) if root else None,
    })
    return 0


def cmd_record(args) -> int:
    root, repo = _root(args), _repo(args)
    asked = now_iso()
    opts = []
    if args.options:
        try:
            raw = json.loads(args.options)
        except ValueError:
            raw = [o.strip() for o in args.options.split("|") if o.strip()]
        for o in raw:
            opts.append(o if isinstance(o, dict) else {"label": str(o), "description": ""})
    rec = {"id": make_id(repo, args.session or "", args.question, asked), "repo": repo, "asked_at": asked,
           "updated_at": asked, "source": args.source, "sources": [{"type": args.source}],
           "topic": args.topic or "", "question": args.question.strip(), "options": opts,
           "answer": args.answer or "", "notes": args.notes or "",
           "custom_answer": bool(opts) and (args.answer or "") not in {o["label"] for o in opts}}
    if not enrolled(root):
        raise SystemExit("this repo has no central list yet: run `bq.py enroll` (or /build-questions:setup)")
    stats = add_to_store(root, repo, [rec])
    print(json.dumps({"id": rec["id"], **stats}))
    return 0


def cmd_import(args) -> int:
    """Merge candidates (a JSON list Claude extracted from stray files or issues) into the central list."""
    root, repo = _root(args), _repo(args)
    raw = json.loads(Path(args.file).read_text())
    raw = raw if isinstance(raw, list) else [raw]
    now = now_iso()
    recs, files = [], {}
    for c in raw:
        if not isinstance(c, dict) or not c.get("question"):
            continue
        src = c.get("source_ref") or {}
        stype = src.get("type") or "file"
        r = {k: c.get(k) for k in ("question", "answer", "topic", "notes", "options") if c.get(k) is not None}
        r["asked_at"] = c.get("asked_at") or now
        r["updated_at"] = c.get("updated_at") or r["asked_at"]
        r["source"] = c.get("source") if c.get("source") in ("prose", "decided", "ask") else "prose"
        r["sources"] = [{"type": stype, **{k: v for k, v in src.items() if k != "type"}}]
        r["id"] = make_id(repo, norm_q(r["question"]))
        recs.append(r)
        if stype == "file" and src.get("path"):
            files[src["path"]] = True
    stats = add_to_store(root, repo, recs)
    if files:
        known = read_sources(root)
        for p in files:
            known[p] = _sha(root / p)
        write_sources(root, known)
    _out({"repo": repo, "candidates": len(recs), **stats, "files_marked": sorted(files)})
    return 0


def cmd_scan(args) -> int:
    root = _root(args)
    if args.mark:
        known = read_sources(root)
        for p in args.mark:
            known[p] = _sha(root / p)
        write_sources(root, known)
        _out({"marked": args.mark})
        return 0
    _out(scan_repo(root, include_known=args.all))
    return 0


def cmd_merge_local(args) -> int:
    """Merge questions captured on this computer (before the repo had a list) and, optionally, transcripts."""
    root, repo = _root(args), _repo(args)
    incoming = read_jsonl(log_path(repo), repo)
    n_local = len(incoming)
    n_tx = 0
    if args.transcripts is not None:
        d = Path(args.transcripts) if args.transcripts else default_transcript_dir(str(root))
        files = sorted(d.glob("*.jsonl")) if d.is_dir() else ([d] if d.is_file() else [])
        for f in files:
            got = records_from_transcript(f, repo)
            n_tx += len(got)
            incoming.extend(got)
    stats = add_to_store(root, repo, incoming)
    if n_local and not args.keep_local:
        log_path(repo).rename(log_path(repo).with_suffix(".merged"))
    _out({"repo": repo, "local": n_local, "transcript": n_tx, **stats})
    return 0


def cmd_list(args) -> int:
    root, repo = _root(args), _repo(args)
    recs = read_store(root, repo)
    if args.format == "json":
        _out(recs)
    else:
        print(render_md(recs, repo))
    return 0


def cmd_diff(args) -> int:
    """ArtifactData batch writes that bring the page up to date with the central list."""
    root, repo = _root(args), _repo(args)
    page = read_page_dir(Path(args.page_dir)) if args.page_dir else {}
    _out(page_diff(read_store(root, repo), page))
    return 0


def cmd_pull(args) -> int:
    """Bring answers changed on the page (newer than the list's copy) back into the central list."""
    root, repo = _root(args), _repo(args)
    page = read_page_dir(Path(args.page_dir))
    have = {r["id"]: r for r in read_store(root, repo)}
    incoming = []
    for doc_id, d in page.items():
        cur = have.get(doc_id)
        if cur is None or stamp(d) > stamp(cur):
            d = {k: v for k, v in d.items() if not k.startswith("_")}
            d.setdefault("id", doc_id)
            d.setdefault("sources", [{"type": "page"}])
            incoming.append(d)
    stats = add_to_store(root, repo, incoming) if incoming else {"added": 0, "merged": 0, "updated": 0}
    _out({"repo": repo, "from_page": len(incoming), **stats})
    return 0


def cmd_synced(args) -> int:
    root, repo = _root(args), _repo(args)
    st = load_state(repo)
    st["synced_sig"] = _store_sig(root)
    save_state(repo, st)
    print("ok")
    return 0


def cmd_set_answer(args) -> int:
    """Change an answer (accepted change request): old answer to history, status changed."""
    root, repo = _root(args), _repo(args)
    recs = read_store(root, repo)
    want = args.question
    tgt = next((r for r in recs if r["id"] == want or want in (r.get("aliases") or [])
                or f"Q{r.get('n', 0):03d}".lower() == want.lower()), None)
    if not tgt:
        raise SystemExit(f"no question {want}")
    now = now_iso()
    hist = list(tgt.get("history") or [])
    hist.append({"answer": tgt.get("answer", ""), "at": now, **({"request_id": args.request} if args.request else {})})
    tgt.update(answer=args.answer, status="changed", history=hist, updated_at=now)
    write_store(root, recs, repo)
    print(json.dumps({"id": tgt["id"], "n": tgt["n"], "answer": args.answer}))
    return 0


def cmd_email(args) -> int:
    cfg = load_config()
    reqs = json.loads(Path(args.requests).read_text())
    reqs = reqs if isinstance(reqs, list) else [reqs]
    repo = args.repo or detect_repo()
    page = args.page or ((cfg.get("projects") or {}).get(repo) or {}).get("artifact") or ""
    subject, body = compose_email(reqs, repo, page)
    if args.dry_run:
        _out({"subject": subject, "body": body})
        return 0
    notify = cfg.get("notify") or {}
    method = args.method or notify.get("method") or "none"
    print(send_email(method, args.to or cfg.get("email") or "", subject, body, args.sender or notify.get("from")))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bq.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("hook-capture", hook_capture), ("hook-session-start", hook_session_start),
                     ("hook-stop", hook_stop), ("hook-stray-write", hook_stray_write)):
        sub.add_parser(name).set_defaults(fn=fn)

    def p_(name, fn, help=None, root=True):
        p = sub.add_parser(name, help=help)
        p.add_argument("--repo", help="owner/repo (default: this checkout's origin)")
        if root:
            p.add_argument("--root", help="repo checkout (default: the current one)")
        p.set_defaults(fn=fn)
        return p

    p_("repo", cmd_repo, "print the owner/repo this checkout records under")
    p = sub.add_parser("config")
    p.add_argument("action", choices=["show", "get", "set"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(fn=cmd_config)
    p = p_("link", cmd_link, "link this repo to its review page", root=False)
    p.add_argument("artifact")
    p.add_argument("--title")
    p_("unlink", cmd_unlink, root=False)
    p_("enroll", cmd_enroll, "create the central list, merge rule and CLAUDE.md block in this repo")
    p = p_("memory", cmd_memory, "write the managed CLAUDE.md block (repo by default; --user for this account)")
    p.add_argument("--user", action="store_true", help="also/only ~/.claude/CLAUDE.md")
    p.add_argument("--repo-memory", action="store_true", help="with --user, also the repo's CLAUDE.md")
    p_("status", cmd_status)

    p = p_("record", cmd_record, "record a question asked in plain text, or a decision made without asking")
    p.add_argument("--question", required=True)
    p.add_argument("--answer", default="")
    p.add_argument("--topic")
    p.add_argument("--options", help='JSON list, or "A|B|C"')
    p.add_argument("--notes")
    p.add_argument("--source", choices=["prose", "decided", "ask"], default="prose")
    p.add_argument("--session")

    p = p_("scan", cmd_scan, "find files holding questions or decisions outside the central list")
    p.add_argument("--all", action="store_true", help="include files already merged and unchanged")
    p.add_argument("--mark", nargs="+", metavar="PATH", help="mark files as reviewed (nothing to import)")
    p = p_("import", cmd_import, "merge extracted candidates (JSON list) into the central list")
    p.add_argument("--file", required=True)
    p = p_("merge-local", cmd_merge_local, "merge this computer's captured questions (and transcripts)")
    p.add_argument("--transcripts", nargs="?", const="", default=None,
                   help="also read transcripts (default folder: this project's)")
    p.add_argument("--keep-local", action="store_true")

    p = p_("list", cmd_list)
    p.add_argument("--format", choices=["md", "json"], default="md")
    p = p_("diff", cmd_diff, "page writes needed (ArtifactData batch entries)")
    p.add_argument("--page-dir", help="folder from `ArtifactData list questions out_dir=...`")
    p = p_("pull", cmd_pull, "bring newer answers from the page into the central list")
    p.add_argument("--page-dir", required=True)
    p_("synced", cmd_synced, "remember that the page now matches the central list")
    p = p_("set-answer", cmd_set_answer, "change an answer after an accepted change request")
    p.add_argument("question", help="id or Q-number (Q014)")
    p.add_argument("answer")
    p.add_argument("--request")

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
