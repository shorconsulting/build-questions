# Changelog

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
