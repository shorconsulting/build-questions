# Changelog

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
