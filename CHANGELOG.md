# Changelog

## 0.7.0 (2026-11-11)

- The list travels with the repo. `enroll` (or `bq.py hooks`) commits a copy of the tool and page template in
  `.claude/build-questions/tool/` and hooks in `.claude/settings.json`, so every account and cloud container runs them
  from the first session, with or without the plugin. Where the plugin is installed too, its hooks stand aside.
- Questions and answers are stored nowhere else. A guard before Write/Edit refuses to put a question or answer list
  outside the list (answers files, question pages kept as JSON or HTML, "Open questions" sections, `Q:`/`A:` lines),
  naming where they go instead; a real FAQ is allowed in `allow.json` or with the `build-questions:allow` marker,
  and ADR folders are left alone. The memory blocks and README say a batch for the owner is the list's review page
  or AskUserQuestion, never a new page or document.
- `scan` also finds answers files and question or answer stores kept as JSON, HTML, CSV or YAML. `import` reports
  which questions got answers (`answered`) and each file's questions as Q-numbers (`by_file`), so organize leaves a
  line where they were saying where the answers now are. The Stop hook asks for strays to be merged.
- The guard refuses only unmistakable question or answer lists that a write adds (open-questions headings,
  `Q:` lines, unchecked `- [ ] ...?` items, `Answer:` lines) and new files named like a question or answer store.
  It never refuses `Decision:` lines, lettered options, ADR or decision-log folders, any `CLAUDE.md`, `.github/` or
  `.claude/`, lines a file already had, or a pointer line in an old store. Refusals and the Stop check apply only
  to a repo set up with `enroll` (committed hooks or the CLAUDE.md block).
- `scan` lists files the way git does (`.gitignore` respected; nested repos, worktrees and their lists skipped).
- The plugin says at session start when a repo's committed copy is older than the plugin.
- The first question captured in a git repo starts that repo's `.claude/build-questions/` list and goes straight
  in; questions wait on this computer (`~/.build-questions/log/`) only when the folder is not a git repo.
- Fix: the README's answered example is valid JSON.

## 0.6.0 (2026-11-11)

- Every question goes in the list as it comes up, from any source, and an answer given anywhere else is written
  back onto its own line, so an answered question never shows as waiting. The repo's and the account's `CLAUDE.md`
  blocks, the list's `README.md` and the skill now say so.
- Open and answered questions have their own formats, spelled out with examples: an open one has 2 to 4 options with
  the recommended one first (`Recommended: `) and notes starting `Recommendation: ... Why: ...`; an answered one
  names who answered, or how to undo a decision Claude made.
- `bq.py record` refuses an open question not in the open format, and builds it from `--recommendation` and
  `--why`. New `bq.py lint` lists open questions not in the format; `bq.py status` counts them.

## 0.5.0 (2026-11-04)

- The repo is the only store of questions. `.claude/build-questions/page.html` is the review page with every
  question built in, regenerated on every write; publishing it is the whole of `sync`. The page's database now
  holds only reviewers' change requests (`bq.py pull` and `diff` are gone).
- Any Claude account puts the latest page up in one call: links live in the committed `page.json` (the main page,
  plus each account's own copy), so no account rebuilds the page or publishes a stray one.
- "Show me the questions" from any account syncs, then opens, that account's page.
- Fixes: `QUESTIONS.md` and the page no longer break on a history entry that records a reworded question (no
  `answer`); the change-request banner no longer shows when hidden.

## 0.4.0 (2026-10-31)

- Runs on macOS, Linux and Windows: hooks use exec form (no shell), the Python command is a plugin option
  (`python3`, or `py` on Windows), and all files are UTF-8 with LF line endings. CI tests all three.
- Email settings are plugin options (secrets in Claude Code's secure storage), sent only by the bundled mail server.
- Privacy policy, terms of use, and a README section on data and the services the plugin talks to.

## 0.3.0

- `/build-questions:schedule` and `/build-questions:run`: organize and present questions on a schedule you choose,
  with an emailed digest.

## 0.2.0

- One central, committed list per repo (`.claude/build-questions/`), managed `CLAUDE.md` blocks,
  `/build-questions:organize` to merge scattered questions, and hooks that keep new ones from straying.

## 0.1.0

- First version: records questions Claude asks, review page with change requests, email notifications.
