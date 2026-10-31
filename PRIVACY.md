# Privacy policy: Build Questions plugin

_Last updated: 31 October 2026_

Build Questions is a Claude Code plugin published by Shor Consulting (David Shor). It runs on your computer or in
your Claude Code session. **Shor Consulting does not operate a server for it and receives no data from it**: no
analytics, no telemetry, no usage reports.

## What the plugin stores, and where

| Data | Where it is kept | Who can see it |
|---|---|---|
| The questions Claude asks while building, the options offered, your answers, and notes | In your repository, in `.claude/build-questions/` (committed with your code) | Anyone with access to that repository |
| Questions captured before a repository has its list, and the plugin's settings (linked review pages, schedule) | On your computer, in `~/.build-questions/` | You |
| A copy of the questions, and reviewers' change requests with their claude.ai user ids | On the review page you publish: a claude.ai artifact under your account, hosted by Anthropic | People you share the page with, under the page's access rules |
| Your notification email address, email method and sender | Claude Code's plugin settings on your computer | You |
| Your Resend API key or SMTP password, if you use email | Claude Code's secure storage (keychain or credentials file) | You; passed only to the plugin's bundled mail server |

The plugin also adds a short block to your repository's `CLAUDE.md` and your account's `~/.claude/CLAUDE.md`, and
can read files and GitHub issues in repositories you work on to find questions. It reads GitHub with your own `gh`
login and never changes or closes issues.

## What leaves your computer

- **Email**, only if you turn it on: change-request notices and scheduled digests, containing question text,
  answers and reviewers' requests, sent to the address you set through the provider you choose (Resend or your SMTP
  server). Their privacy policies apply to that mail. With the `github` method, the same text is posted as a comment
  on an issue in your repository instead.
- **The review page**, if you publish one: stored by Anthropic as a claude.ai artifact under your account.
  [Anthropic's privacy policy](https://www.anthropic.com/legal/privacy) applies.
- **Your repository**, when you or Claude push commits that include `.claude/build-questions/`.

Nothing else is sent anywhere.

## Your choices

Turn email off by setting the method to `none`. Stop the schedule with `/build-questions:schedule stop`. Delete
the review page from claude.ai. Remove `.claude/build-questions/`, the `CLAUDE.md` blocks and `~/.build-questions/`
to remove everything the plugin wrote. Uninstalling the plugin removes its options and stored secrets from Claude
Code.

## Contact

Questions about this policy: [open an issue](https://github.com/shorconsulting/build-questions/issues).
