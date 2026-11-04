#!/usr/bin/env python3
"""build-questions: one central, committed list of every question asked while building an app.

Stdlib only.

THE CENTRAL LIST (source of truth, committed in each repo)
  <repo>/.claude/build-questions/questions.jsonl   one JSON object per line, one line per question
  <repo>/.claude/build-questions/QUESTIONS.md      readable copy, regenerated on every write (don't edit)
  <repo>/.claude/build-questions/README.md         the line format, for sessions without the plugin
  <repo>/.claude/build-questions/sources.json      files already merged in, with their content hash
  <repo>/.claude/build-questions/page.json         review page links: the main one, each account's copy, capabilities
  <repo>/.claude/build-questions/page.html         the page itself with every question built in, regenerated on every
                                                   write: any account publishes it as-is and it shows the latest list
  <repo>/.gitattributes                             "questions.jsonl merge=union" so branches merge cleanly
  <repo>/CLAUDE.md                                  managed block telling every session where questions go

  A line may repeat an id (union merges, appends without the plugin): the copy with the latest
  `updated_at` wins. Lines without an id get one from their question text.

LOCAL (per computer, under $BUILD_QUESTIONS_HOME, default ~/.build-questions)
  config.json                 linked projects {owner/repo: {artifact, title}} (fallback for page.json), schedule
  log/<owner__repo>.jsonl     questions captured in repos not enrolled yet (merged in by `organize`)
  state/<owner__repo>.json    hook bookkeeping

THE PAGE (claude.ai artifact) is the review surface: page.html published as-is. Its database holds only reviewers'
change requests; the questions are built into the HTML, so the repo stays the only store of questions.

EMAIL is not sent from here: `email` and `digest` print {subject, body}, and Claude sends it with the bundled mail
server (scripts/mail_server.py), which gets its settings from the plugin's options. This script reads no
credentials and makes no network calls.

Hooks never fail the session: every hook entry point swallows errors and exits 0.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import hashlib
import html
import json
import os
import re
import subprocess
import sys
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
        return json.loads((home() / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    p = home() / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
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


def page_file(root: Path) -> Path:
    return store_dir(root) / "page.json"


def page_html_file(root: Path) -> Path:
    return store_dir(root) / "page.html"


PAGE_TEMPLATE = Path(__file__).resolve().parent.parent / "page" / "build-questions.html"
PAGE_CAPABILITIES = {"db": {"rules": [{"path": "", "read": "view", "write": "admin"},
                                      {"path": "requests", "read": "view", "write": "interact"},
                                      {"path": "settings", "read": "admin", "write": "admin"}]},
                     "user": {"scopes": ["profile"]}}


def load_page_meta(root: Path | None) -> dict:
    """page.json: {artifact (main page, shared with reviewers), accounts {email: url}, title, capabilities}."""
    try:
        d = json.loads(page_file(root).read_text(encoding="utf-8")) if root else {}
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_page_meta(root: Path, **fields) -> dict:
    d = load_page_meta(root)
    d.update({k: v for k, v in fields.items() if v is not None})
    d["capabilities"] = PAGE_CAPABILITIES
    page_file(root).write_text(json.dumps(d, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return d


def page_link(repo: str, root: Path | None = None, account: str | None = None) -> str | None:
    """This account's copy, else the main page (committed in the repo, so every account sees it), else local config."""
    meta = load_page_meta(root)
    url = ((meta.get("accounts") or {}).get(account) if account else None) or meta.get("artifact")
    return url or ((load_config().get("projects") or {}).get(repo) or {}).get("artifact")


def page_title(repo: str, root: Path | None) -> str:
    return (load_page_meta(root).get("title") or ((load_config().get("projects") or {}).get(repo) or {}).get("title")
            or repo.split("/")[-1])


def _seed_json(recs: list[dict], repo: str, title: str) -> str:
    # One question per line keeps diffs readable; "<" escaped so no text can close the <script> tag.
    meta = {"repo": repo, "title": title, "count": max((r.get("n") or 0 for r in recs), default=0),
            "updated_at": max((stamp(r) for r in recs), default="")}
    qs = ",\n".join(json.dumps(page_doc(r), sort_keys=True, ensure_ascii=False) for r in recs)
    return ('{"meta": ' + json.dumps(meta, sort_keys=True, ensure_ascii=False) + ',\n"questions": [\n' + qs
            + "\n]}").replace("<", "\\u003c")


def render_page(recs: list[dict], repo: str, title: str) -> str:
    tpl = PAGE_TEMPLATE.read_text(encoding="utf-8")
    tpl = tpl.replace("<title>Build Questions</title>", f"<title>{html.escape(title)} Build Questions</title>", 1)
    seed = f'<script type="application/json" id="bq-seed">\n{_seed_json(recs, repo, title)}\n</script>\n'
    i = tpl.index("<script>")
    return tpl[:i] + seed + tpl[i:]


def load_state(repo: str) -> dict:
    try:
        return json.loads(state_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(repo: str, st: dict) -> None:
    p = state_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st), encoding="utf-8", newline="\n")


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
        with Path(path).open(encoding="utf-8") as f:
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
    tmp.write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8", newline="\n")
    tmp.replace(store_file(root))
    (d / "QUESTIONS.md").write_text(render_md(recs, repo, root), encoding="utf-8", newline="\n")
    if PAGE_TEMPLATE.exists():
        page_html_file(root).write_text(render_page(recs, repo, page_title(repo, root)), encoding="utf-8", newline="\n")


def add_to_store(root: Path, repo: str, incoming: list[dict]) -> dict:
    recs, stats = merge_records(read_store(root, repo), [clean(r, repo) for r in incoming if clean(r, repo)])
    write_store(root, recs, repo)
    return stats


def render_md(recs: list[dict], repo: str, root: Path | None = None) -> str:
    page = page_link(repo, root)
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
            # history can also hold reworded questions ({at, question}); show the last earlier answer
            prev = [h["answer"] for h in r.get("history") or [] if isinstance(h, dict) and h.get("answer")]
            was = f" (was: ~~{prev[-1]}~~)" if prev else ""
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

## The review page

`page.html` is the review page (a claude.ai artifact) with every question in this list built in, regenerated by
the plugin. To put the latest page up from any Claude account, publish `page.html` as it is, with the
`capabilities` in `page.json`; then add the link to `page.json` under `accounts` (your email). `artifact` there is
the main page, the one shared with reviewers. The page's own database holds only reviewers' change requests.

## Don't keep questions anywhere else

No QUESTIONS.md / DECISIONS.md elsewhere, no "Open questions" sections in plans or specs, no question-only
issues. If you find one, move its questions here (`/build-questions:organize`) and leave a link to this folder.
"""


def ensure_gitattributes(root: Path) -> bool:
    p = Path(root) / ".gitattributes"
    line = f"{STORE_REL.as_posix()}/questions.jsonl merge=union"
    try:
        cur = p.read_text(encoding="utf-8")
    except OSError:
        cur = ""
    if line in cur:
        return False
    p.write_text(cur + ("" if not cur or cur.endswith("\n") else "\n") + line + "\n", encoding="utf-8", newline="\n")
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
        cur = path.read_text(encoding="utf-8")
    except OSError:
        cur = None
    if cur is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block + "\n", encoding="utf-8", newline="\n")
        return "created"
    pat = re.compile(re.escape(MEM_START) + r".*?" + re.escape(MEM_END), re.S)
    new = pat.sub(lambda _m: block, cur, count=1) if pat.search(cur) else \
        cur + ("" if cur.endswith("\n") else "\n") + ("\n" if cur.strip() else "") + block + "\n"
    if new == cur:
        return "unchanged"
    path.write_text(new, encoding="utf-8", newline="\n")
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
        return json.loads((store_dir(root) / "sources.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_sources(root: Path, src: dict) -> None:
    store_dir(root).mkdir(parents=True, exist_ok=True)
    (store_dir(root) / "sources.json").write_text(json.dumps(src, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def scan_file(path: Path, rel: str) -> list[dict]:
    hits = []
    try:
        if path.stat().st_size > 1_000_000:
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
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
    """Hook payloads are UTF-8 JSON on every platform; read bytes so Windows' console code page can't garble them."""
    try:
        buf = getattr(sys.stdin, "buffer", None)
        raw = buf.read().decode("utf-8", errors="replace") if buf else sys.stdin.read()
        return json.loads(raw or "{}")
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
            with p.open("a", encoding="utf-8", newline="\n") as f:
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
        page = page_link(repo, root)
        local = read_jsonl(log_path(repo), repo)
        if enrolled(root):
            if page and not load_page_meta(root).get("artifact"):
                # Linked before page.json existed: commit the link so other accounts stop publishing their own page.
                save_page_meta(root, artifact=page, title=page_title(repo, root))
            recs = read_store(root, repo)
            n_open = sum(1 for r in recs if not r.get("answer"))
            strays = scan_repo(root, limit_files=1500)
            msg = (f"build-questions: this repo's central list of product questions is "
                   f"{STORE_REL.as_posix()}/questions.jsonl ({len(recs)} questions, {n_open} unanswered; read "
                   f"QUESTIONS.md beside it before asking anything).")
            if page:
                msg += f" Review page: {page}."
            todo = []
            if local:
                todo.append(f"{len(local)} question(s) captured before the list existed")
            if strays:
                todo.append(f"{len(strays)} file(s) with questions outside the list ("
                            + ", ".join(s["path"] for s in strays[:5]) + ("…" if len(strays) > 5 else "") + ")")
            if todo:
                msg += " To merge: " + "; ".join(todo) + ". Run the build-questions 'organize' steps before " \
                       "other work, and include the result in your next commit."
            if page:
                msg += " Then follow the skill's 'Start of work' step (sync, check for change requests)."
            _emit("SessionStart", msg)
        elif local or page:
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
        page = page_link(repo, root)
        st = load_state(repo)
        sig = _store_sig(root)
        dirty = bool(_git(root, "status", "--porcelain", "--", STORE_REL.as_posix()))
        need_sync = bool(page) and st.get("synced_sig") != sig
        if not (need_sync or dirty):
            return 0
        remind = f"{sig}:{int(need_sync)}:{int(dirty)}"
        if st.get("last_stop_reminder") == remind:
            return 0
        st["last_stop_reminder"] = remind
        save_state(repo, st)
        steps = []
        if need_sync:
            steps.append("republish .claude/build-questions/page.html to the review page (docs/commands.md 'sync')")
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
        with p.open(encoding="utf-8", errors="replace") as f:
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

PAGE_FIELDS = ("id", "n", "repo", "asked_at", "updated_at", "source", "topic", "question", "options", "multi",
               "answer", "custom_answer", "notes", "status", "history", "aliases")


def page_doc(r: dict) -> dict:
    d = {k: r.get(k) for k in PAGE_FIELDS if r.get(k) is not None}
    d["recorded_by"] = "build-questions"
    return d


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


def compose_digest(recs: list[dict], repo: str, page: str, since: str, requests: list[dict] | None = None,
                   organized: dict | None = None) -> tuple[str, str]:
    """The scheduled summary: what's new or changed since the last digest, what's waiting, change requests."""
    new = [r for r in recs if (r.get("asked_at") or "") > since]
    changed = [r for r in recs if r not in new and (r.get("updated_at") or "") > since and r.get("history")]
    waiting = [r for r in recs if not r.get("answer")]
    reqs = [q for q in (requests or []) if q.get("status") in ("open", "accepted")]
    q = lambda r: f"Q{r.get('n', 0):03d}"
    parts = []
    if new:
        parts.append(f"{len(new)} new")
    if changed:
        parts.append(f"{len(changed)} changed")
    if reqs:
        parts.append(f"{len(reqs)} change request{'s' if len(reqs) != 1 else ''}")
    if waiting:
        parts.append(f"{len(waiting)} waiting for an answer")
    subject = f"[{repo}] Build questions: " + (", ".join(parts) if parts else "nothing new")
    lines = [f"Build questions for {repo}" + (f" since {since[:10]}" if since else "") + ".", ""]
    if organized and any(organized.get(k) for k in ("added", "merged", "files")):
        lines += [f"Organized: {organized.get('added', 0)} found in other places, {organized.get('merged', 0)} "
                  f"duplicates merged, {organized.get('files', 0)} files cleaned up.", ""]
    if reqs:
        lines.append("CHANGE REQUESTS")
        for r in reqs:
            lines.append(f"  {r.get('question_id', '')}: {r.get('question', '').strip()}")
            lines.append(f"    Now: {r.get('current_answer') or '(none)'} -> Proposed: {r.get('proposed') or '?'}"
                         f" [{r.get('status')}]")
        lines.append("")
    if waiting:
        lines.append("WAITING FOR AN ANSWER")
        lines += [f"  {q(r)}{' · ' + r['topic'] if r.get('topic') else ''}: {r['question']}" for r in waiting]
        lines.append("")
    if new:
        lines.append("NEW")
        lines += [f"  {q(r)} {r['question']}\n    -> {r.get('answer') or '(no answer yet)'}" for r in new]
        lines.append("")
    if changed:
        lines.append("CHANGED")
        lines += [f"  {q(r)} {r['question']}\n    -> {r['answer']} (was: {r['history'][-1]['answer']})" for r in changed]
        lines.append("")
    if not (new or changed or waiting or reqs):
        lines += ["Nothing new, nothing waiting.", ""]
    lines.append(f"{len(recs)} questions in total.")
    if page:
        lines.append(f"Review page: {page}")
    lines += ["", "Sent on your schedule by the build-questions plugin (/build-questions:schedule to change it)."]
    return subject, "\n".join(lines)


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
    root = repo_root(getattr(args, "root", None))
    main = not args.account or not page_link(repo, root)  # the first page linked becomes the main one
    proj = cfg.setdefault("projects", {}).setdefault(repo, {})
    if main:
        proj["artifact"] = args.artifact
    if args.title:
        proj["title"] = args.title
    save_config(cfg)
    if enrolled(root):
        # Committed with the list, so another account finds this page instead of publishing its own.
        accounts = dict(load_page_meta(root).get("accounts") or {})
        if args.account:
            accounts[args.account] = args.artifact
        save_page_meta(root, artifact=args.artifact if main else None, title=args.title, accounts=accounts or None)
        upsert_block(root / "CLAUDE.md", repo_memory_block(repo, page_link(repo, root)))
        write_store(root, read_store(root, repo), repo)
    print(f"{repo} -> {args.artifact}")
    return 0


def cmd_unlink(args) -> int:
    cfg = load_config()
    repo = _repo(args)
    (cfg.get("projects") or {}).pop(repo, None)
    save_config(cfg)
    root = repo_root(getattr(args, "root", None))
    meta = load_page_meta(root)
    if meta:
        meta.pop("artifact", None)
        meta.pop("accounts", None)
        page_file(root).write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"{repo} unlinked")
    return 0


def cmd_enroll(args) -> int:
    """Create the central list, the merge rule and the repo's memory block. Idempotent."""
    root, repo = _root(args), _repo(args)
    d = store_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    if args.title:
        save_page_meta(root, title=args.title)
    created = not store_file(root).exists()
    if created:
        store_file(root).write_text("", encoding="utf-8", newline="\n")
    (d / "README.md").write_text(STORE_README, encoding="utf-8", newline="\n")
    recs = read_store(root, repo)
    write_store(root, recs, repo)
    ga = ensure_gitattributes(root)
    page = page_link(repo, root)
    mem = upsert_block(root / "CLAUDE.md", repo_memory_block(repo, page))
    _out({"repo": repo, "store": str(store_file(root)), "created": created, "gitattributes_changed": ga,
          "claude_md": mem, "page_html": str(page_html_file(root))})
    return 0


def cmd_memory(args) -> int:
    res = {}
    if args.user:
        res["user"] = upsert_block(Path.home() / ".claude" / "CLAUDE.md", USER_MEMORY_BLOCK)
    if args.repo_memory or not args.user:
        root, repo = _root(args), _repo(args)
        page = page_link(repo, root)
        res["repo"] = upsert_block(root / "CLAUDE.md", repo_memory_block(repo, page))
    _out(res)
    return 0


def cmd_status(args) -> int:
    repo = _repo(args)
    root = repo_root(getattr(args, "root", None))
    recs = read_store(root, repo) if enrolled(root) else []
    _out({
        "repo": repo, "root": str(root) if root else None, "enrolled": enrolled(root),
        "page": page_link(repo, root, args.account),
        "main_page": page_link(repo, root),
        "page_html": str(page_html_file(root)) if enrolled(root) else None,
        "page_title": f"{page_title(repo, root)} Build Questions",
        "capabilities": PAGE_CAPABILITIES,
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
    raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
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
        log_path(repo).replace(log_path(repo).with_suffix(".merged"))
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


def cmd_digest(args) -> int:
    """Print the digest as {subject, body}; Claude sends it with the mail server's send_email tool."""
    root, repo = _root(args), _repo(args)
    page = args.page or page_link(repo, root) or ""
    reqs = json.loads(Path(args.requests).read_text(encoding="utf-8")) if args.requests else []
    organized = json.loads(args.organized) if args.organized else None
    subject, body = compose_digest(read_store(root, repo), repo, page, args.since or "", reqs, organized)
    _out({"subject": subject, "body": body})
    return 0


def cmd_schedule(args) -> int:
    """Remember the schedule the person chose (the scheduled task itself is created by Claude)."""
    cfg = load_config()
    if args.action == "set":
        cfg["schedule"] = {k: v for k, v in {
            "cron": args.cron, "tz": args.tz, "label": args.label, "repos": args.repos,
            "steps": args.steps, "task_id": args.task_id, "where": args.where, "updated_at": now_iso()}.items() if v}
        save_config(cfg)
    elif args.action == "clear":
        cfg.pop("schedule", None)
        save_config(cfg)
    _out(cfg.get("schedule") or {})
    return 0


def cmd_email(args) -> int:
    """Print the change-request email as {subject, body}; Claude sends it with the mail server's send_email tool."""
    reqs = json.loads(Path(args.requests).read_text(encoding="utf-8"))
    reqs = reqs if isinstance(reqs, list) else [reqs]
    repo = args.repo or detect_repo()
    page = args.page or page_link(repo, repo_root()) or ""
    subject, body = compose_email(reqs, repo, page)
    _out({"subject": subject, "body": body})
    return 0


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _utf8_stdio()
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
    p = p_("link", cmd_link, "link this repo to its review page (committed in page.json)")
    p.add_argument("artifact")
    p.add_argument("--title")
    p.add_argument("--account", help="your claude.ai email: links this account's own copy of the page")
    p_("unlink", cmd_unlink)
    p = p_("enroll", cmd_enroll, "create the central list, merge rule and CLAUDE.md block in this repo")
    p.add_argument("--title", help="project name, for the page's title")
    p = p_("memory", cmd_memory, "write the managed CLAUDE.md block (repo by default; --user for this account)")
    p.add_argument("--user", action="store_true", help="also/only ~/.claude/CLAUDE.md")
    p.add_argument("--repo-memory", action="store_true", help="with --user, also the repo's CLAUDE.md")
    p = p_("status", cmd_status)
    p.add_argument("--account", help="your claude.ai email: `page` is then this account's copy, if it has one")

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
    p_("synced", cmd_synced, "remember that page.html is now published to the page")
    p = p_("set-answer", cmd_set_answer, "change an answer after an accepted change request")
    p.add_argument("question", help="id or Q-number (Q014)")
    p.add_argument("answer")
    p.add_argument("--request")

    p = p_("digest", cmd_digest, "the scheduled summary of new, changed and waiting questions")
    p.add_argument("--since", help="ISO time of the last digest")
    p.add_argument("--requests", help="JSON file of change requests from the page")
    p.add_argument("--organized", help='JSON stats from organize, e.g. {"added":2,"merged":1,"files":1}')
    p.add_argument("--page")
    p = sub.add_parser("schedule", help="remember the organize-and-present schedule")
    p.add_argument("action", choices=["show", "set", "clear"])
    p.add_argument("--cron")
    p.add_argument("--tz")
    p.add_argument("--label", help='how to say it, e.g. "Mondays and Thursdays at 8:40"')
    p.add_argument("--repos", help="comma-separated owner/repo list, or all-linked")
    p.add_argument("--steps", help="comma-separated: organize,sync,check,digest")
    p.add_argument("--task-id")
    p.add_argument("--where", choices=["cloud", "desktop", "session"])
    p.set_defaults(fn=cmd_schedule)

    p = sub.add_parser("email", help="the change-request email, as {subject, body}")
    p.add_argument("--requests", required=True, help="JSON file: one request or a list")
    p.add_argument("--repo")
    p.add_argument("--page")
    p.set_defaults(fn=cmd_email)

    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
