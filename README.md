# build-questions

A Claude Code plugin that keeps one central, reviewable list of every decision made while building an app.

- **One list per repo, committed.** Every product question and its answer lives in
  `.claude/build-questions/questions.jsonl` (readable copy `QUESTIONS.md`), shared by every Claude account that
  works on the repo. Branches merge it without conflicts.
- **Records** every question Claude asks with `AskUserQuestion` (options and answer included) by a hook, so nothing
  depends on Claude remembering. Questions asked in plain chat, and decisions Claude made without asking, are
  recorded by the skill.
- **Tells every account where questions go.** A managed block in each repo's `CLAUDE.md` and in each account's
  `~/.claude/CLAUDE.md`: read the list before asking, record there, never start another list. Sessions without
  the plugin can still follow it: the folder's `README.md` gives the line format.
- **Merges the mess.** `/build-questions:organize` gathers questions from stray files (plans, specs, notes,
  QUESTIONS.md, DECISIONS.md, "Open questions" sections), GitHub issues, past sessions and this computer into the
  list, merges duplicates (newer answer wins, older kept as history), replaces the stray lists with a link, and
  refreshes memory. Run it on one repo, several, or every linked repo.
- **Keeps it tidy.** Hooks flag any edit that starts a question list elsewhere, report strays at session start, and
  remind Claude to commit and sync the list before ending a turn.
- **Publishes** it on a review page per project (a claude.ai artifact) with two tabs: **Open questions** (no
  answer yet, or a change request pending) as full cards, and **Settled questions** as one compact row each:
  question, answer, and a Request a change button.
- **Runs on your schedule.** `/build-questions:schedule` sets when questions are organized and presented to you
  (weekday mornings, twice a week, weekly, any time you name). Each run merges strays, updates the page, checks
  for change requests and emails a digest: change requests, questions waiting for an answer, and what's new or
  changed since the last one.
- **Notifies the owner** of change requests by email (the address chosen at setup) and in the interface: a banner
  and the **Open questions** tab on the page, a note at the start of every Claude session in the repo, and
  `/build-questions:changes` in chat.
- **Closes the loop**: the owner accepts or keeps each request (on the page or in chat); Claude makes accepted changes
  in the build, records the new answer (keeping the old one), and links the PR.

See [CONCEPT.md](CONCEPT.md) for the design and the open questions.

## Install

```
/plugin marketplace add shorconsulting/build-questions
/plugin install build-questions@build-questions
```

Shor Consulting accounts can also install it from the shared marketplace:
`/plugin marketplace add shorconsulting/marketplace`, then `/plugin install build-questions@shorconsulting`.

Needs `python3` and `git` (`gh` for issues). Then, inside the repo being built: `/build-questions:setup`.

## Commands

| Command | What it does |
|---|---|
| `/build-questions:setup` | Creates the repo's central list and memory block, sets the notification email, links a review page (or publishes one), sets your schedule |
| `/build-questions:organize [owner/repo ...] [--all-linked]` | Merges questions from stray files, issues, past sessions and this computer into each repo's list, cleans up the strays, refreshes memory, commits |
| `/build-questions:schedule [when \| show \| stop]` | Choose when questions are organized and presented to you (e.g. weekday mornings, Mondays 8am); creates the scheduled task |
| `/build-questions:run` | One scheduled run: organize, sync, check, and email the digest (what the schedule fires; also works by hand) |
| `/build-questions:sync` | Makes the review page match the central list, both ways |
| `/build-questions:check` | Finds new change requests, emails the owner, reports. Safe unattended: use it as the scheduled task |
| `/build-questions:changes [--all] [--page]` | Lists requested changes in chat, or opens the page on its Open questions tab |
| `/build-questions:resolve [Q014]` | Asks the owner to accept or keep each request, then applies the accepted ones |

## The central list

```
.claude/build-questions/
  questions.jsonl   one JSON object per question (source of truth; merge=union in .gitattributes)
  QUESTIONS.md      readable copy, regenerated on every write
  README.md         the line format, for sessions without the plugin
  sources.json      stray files already merged, with their content hash
CLAUDE.md           managed block between <!-- build-questions:start --> and <!-- build-questions:end -->
```

Each record: `id`, `n` (Q-number), `question`, `answer`, `topic`, `options`, `source` (`ask`, `prose`,
`decided`), `asked_at`, `updated_at`, `status`, `history` (earlier answers), `sources` (where it was found),
`aliases` (ids of duplicates merged into it).

## Email

Set in the plugin's options, which Claude Code asks for when you enable the plugin (change them later with
`/plugin` → build-questions → Configure, or `claude plugin configure build-questions`): where notices and digests
go, how they are sent (`resend`, `smtp`, `github` for an @-mention on an issue, or `none`), an optional sender,
and the details the chosen method needs. Secret values stay in Claude Code's secure storage.

Mail is sent only by the bundled `mail` MCP server (`scripts/mail_server.py`), which receives these options from
Claude Code and talks only to the provider you chose. Nothing else in the plugin sends anything off the machine.
Emails go out on the schedule you choose (`/build-questions:schedule`) and when someone starts build work in the
repo.

## Who can do what on the page

| Access on the artifact | Can |
|---|---|
| Viewer | Read questions, answers and requests |
| Contributor ("can interact") | Also request changes and withdraw their own open requests |
| Editor / owner | Also accept or keep requests; sees the notification settings |

## Files

```
.claude-plugin/plugin.json      manifest (marketplace.json beside it: this repo is its own marketplace)
hooks/hooks.json                capture (AskUserQuestion), stray check (Write/Edit), SessionStart note, Stop reminder
scripts/bq.py                   central list, merge, scan, memory, hooks, page diff, email text (stdlib, no network)
scripts/mail_server.py          the only part that sends mail: a small MCP server fed by the plugin's options
scripts/test_bq.py              unit tests
skills/build-questions/SKILL.md how Claude asks, records and handles change requests
commands/*.md                   the slash commands; all read docs/commands.md
docs/commands.md                the shared guide: the page's store and each command's steps
page/build-questions.html       the review page template
```

Tests: `python3 -m unittest discover -s scripts -p 'test_*.py'`.

## License

MIT. See [LICENSE](LICENSE).
