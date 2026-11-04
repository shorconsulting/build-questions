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

Then, inside the repo being built: `/build-questions:setup`.

## Platforms

Works on **macOS, Linux and Windows**. It needs Python 3.10 or newer and `git` (and `gh` for GitHub issues).

| | Python command (`python` option) | Get Python |
|---|---|---|
| macOS | `python3` | `xcode-select --install` or `brew install python` |
| Linux | `python3` | the distribution's `python3` package |
| Windows | `py` (or `python`) | `winget install Python.Python.3.13` |

Claude Code asks for the `python` option when you enable the plugin (`python3` is filled in; Windows users change
it to `py`), and `/build-questions:setup` checks it and fixes it if needed. Hooks run in exec form, without a shell,
so they behave the same under bash, zsh, Git Bash and PowerShell. Files are read and written as UTF-8 with `\n`
line endings on every platform, so the committed list doesn't churn between Windows and macOS teammates. The test
suite runs on all three in CI.

## Commands

| Command | What it does |
|---|---|
| `/build-questions:setup` | Creates the repo's central list and memory block, sets the notification email, links a review page (or publishes one), sets your schedule |
| `/build-questions:organize [owner/repo ...] [--all-linked]` | Merges questions from stray files, issues, past sessions and this computer into each repo's list, cleans up the strays, refreshes memory, commits |
| `/build-questions:schedule [when \| show \| stop]` | Choose when questions are organized and presented to you (e.g. weekday mornings, Mondays 8am); creates the scheduled task |
| `/build-questions:run` | One scheduled run: organize, sync, check, and email the digest (what the schedule fires; also works by hand) |
| `/build-questions:sync` | Republishes the review page from the repo's `page.html` (every question built in) |
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

## Data and the services it talks to

Shor Consulting runs no server for this plugin and receives no data from it. Everything stays with you, except
what you send through these services:

| Service | When | What is sent |
|---|---|---|
| Resend (api.resend.com) | Only if `email_method` is `resend` | Change-request notices and digests (question text, answers, reviewers' requests) to your notification address |
| Your SMTP server | Only if `email_method` is `smtp` | The same notices and digests |
| GitHub | Reading issues during `organize`; posting a comment if `email_method` is `github`; your own pushes | Issue lookups with your `gh` login; the notice or digest text as an issue comment |
| claude.ai (Anthropic) | If you publish a review page | The question list and reviewers' change requests, stored as an artifact under your account |

Personal data it stores: your notification email address (Claude Code's plugin settings), reviewers' claude.ai user
ids on the review page, and whatever names appear in your questions and answers. See the
[privacy policy](PRIVACY.md).

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
scripts/bq.py                   central list, merge, scan, memory, hooks, page.html, email text (stdlib, no network)
scripts/mail_server.py          the only part that sends mail: a small MCP server fed by the plugin's options
scripts/test_bq.py              unit tests
skills/build-questions/SKILL.md how Claude asks, records and handles change requests
commands/*.md                   the slash commands; all read docs/commands.md
docs/commands.md                the shared guide: the page's store and each command's steps
page/build-questions.html       the review page template
```

Tests: `python3 -m unittest discover -s scripts -p 'test_*.py'` (Windows: `py -m unittest ...`). CI runs them on
Linux, macOS and Windows with Python 3.10 and 3.13.

## License, privacy and support

Changes: [CHANGELOG.md](CHANGELOG.md). MIT. See [LICENSE](LICENSE), the [privacy policy](PRIVACY.md) and the [terms of use](TERMS.md). Support:
[GitHub issues](https://github.com/shorconsulting/build-questions/issues).
