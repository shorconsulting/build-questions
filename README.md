# build-questions

A Claude Code plugin that keeps a reviewable record of every decision made while building an app.

- **Records** every question Claude asks (with `AskUserQuestion`), the options it offered and the answer, by a
  hook, so nothing depends on Claude remembering. Questions asked in plain chat, and decisions Claude made without
  asking, are recorded by the skill.
- **Publishes** them on a review page per project (a claude.ai artifact) with two tabs: **Open questions** (no
  answer yet, or a change request pending) as full cards, and **Settled questions** as one compact row each:
  question, answer, and a Request a change button.
- **Lets reviewers request a change** to any answer: proposed answer and reason.
- **Notifies the owner** by email (the address chosen at setup) and in the interface: a banner and the
  **Open questions** tab on the page, a note at the start of every Claude session in the repo, and
  `/build-questions:changes` in chat.
- **Closes the loop**: the owner accepts or keeps each request (on the page or in chat); Claude makes accepted changes
  in the build, marks the answer changed (keeping the old one), and links the PR.

See [CONCEPT.md](CONCEPT.md) for the design and the open questions.

## Install

```
/plugin marketplace add shorconsulting/marketplace
/plugin install build-questions@shorconsulting
```

Needs `python3` and `git`. Then, inside the repo being built: `/build-questions:setup`.

## Commands

| Command | What it does |
|---|---|
| `/build-questions:setup` | Sets the notification email and how it's sent, links the repo to a review page (or publishes one), offers the hourly check |
| `/build-questions:sync` | Copies recorded questions that aren't on the page yet |
| `/build-questions:check` | Finds new change requests, emails the owner, reports. Safe unattended: use it as the scheduled task |
| `/build-questions:changes [--all] [--page]` | Lists requested changes in chat, or opens the page on its Open questions tab |
| `/build-questions:resolve [Q014]` | Asks the owner to accept or keep each request, then applies the accepted ones |
| `/build-questions:backfill` | Recovers questions from this project's past transcripts |
| `/build-questions:export [path]` | Writes the decision log into the repo as Markdown |

## Email

Chosen at setup (`notify.method` in `~/.build-questions/config.json`):

| Method | Needs | Notes |
|---|---|---|
| `resend` | `RESEND_API_KEY` | Recommended. Test sender delivers only to the Resend account's address until a domain is verified |
| `smtp` | `BQ_SMTP_HOST`, `BQ_SMTP_PORT`, `BQ_SMTP_USER`, `BQ_SMTP_PASSWORD`, `BQ_SMTP_FROM` | Any mailbox |
| `github` | nothing | Claude @-mentions you on one issue in the repo; GitHub emails your GitHub address |
| `none` | | Page and chat only |

Emails go out when `check` runs: hourly from the scheduled task setup offers, and at the start of build work in the
repo. Credentials are never written to the page.

## Who can do what on the page

| Access on the artifact | Can |
|---|---|
| Viewer | Read questions, answers and requests |
| Contributor ("can interact") | Also request changes and withdraw their own open requests |
| Editor / owner | Also accept or keep requests; sees the notification settings |

## Files

```
.claude-plugin/plugin.json      manifest
hooks/hooks.json                PostToolUse(AskUserQuestion) capture, SessionStart note, Stop sync reminder
scripts/bq.py                   local store, hooks, backfill, email (stdlib only)
scripts/test_bq.py              unit tests
skills/build-questions/SKILL.md how Claude asks, records and handles change requests
commands/*.md                   the slash commands; all read docs/commands.md
docs/commands.md                the shared guide: the page's store and each command's steps
page/build-questions.html       the review page template
```

Tests: `python3 -m unittest discover -s scripts -p 'test_*.py'`.
