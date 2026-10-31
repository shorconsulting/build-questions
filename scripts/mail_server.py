#!/usr/bin/env python3
"""build-questions mail: a tiny MCP server (stdio) that sends the plugin's emails.

It is the only part of the plugin that holds an email credential or talks to the network. Its settings come
from the plugin's options (plugin.json `userConfig`), which Claude Code asks for when the plugin is enabled:
sensitive ones (API key, SMTP password) are kept in secure storage and passed here as environment variables.
It never reads credentials from files or the user's shell. Stdlib only.

Tools:
  email_status  which method is set up and where mail goes (no secrets)
  send_email    send one plain-text email to the configured address
"""
from __future__ import annotations

import json
import smtplib
import sys
import urllib.request
from email.message import EmailMessage
import os

VERSION = "0.4.0"


def opt(name: str) -> str:
    v = os.environ.get(name, "").strip()
    return "" if v.startswith("${") else v  # an option left unset can arrive unsubstituted


def settings() -> dict:
    return {
        "to": opt("BQ_EMAIL"),
        "method": (opt("BQ_METHOD") or "none").lower(),
        "from": opt("BQ_FROM"),
        "resend_key": opt("BQ_RESEND_API_KEY"),
        "smtp_host": opt("BQ_SMTP_HOST"),
        "smtp_port": opt("BQ_SMTP_PORT") or "587",
        "smtp_user": opt("BQ_SMTP_USER"),
        "smtp_password": opt("BQ_SMTP_PASSWORD"),
    }


def status() -> dict:
    s = settings()
    missing = []
    if not s["to"]:
        missing.append("notify_email")
    if s["method"] == "resend" and not s["resend_key"]:
        missing.append("resend_api_key")
    if s["method"] == "smtp":
        missing += [k for k, v in (("smtp_host", s["smtp_host"]), ("smtp_password", s["smtp_password"])) if not v]
    return {"method": s["method"], "to": s["to"], "from": s["from"] or None,
            "ready": s["method"] in ("resend", "smtp") and not missing, "missing_options": missing}


def send(subject: str, body: str) -> str:
    s = settings()
    st = status()
    if s["method"] not in ("resend", "smtp"):
        raise ValueError(f"email method is '{s['method']}', so nothing is sent by email")
    if not st["ready"]:
        raise ValueError("set these plugin options first: " + ", ".join(st["missing_options"]))
    if s["method"] == "resend":
        payload = json.dumps({"from": s["from"] or "Build Questions <onboarding@resend.dev>", "to": [s["to"]],
                              "subject": subject, "text": body}).encode()
        req = urllib.request.Request("https://api.resend.com/emails", data=payload, method="POST", headers={
            "Authorization": f"Bearer {s['resend_key']}", "Content-Type": "application/json",
            "User-Agent": f"build-questions/{VERSION}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return "Sent with Resend (id " + str(json.loads(resp.read() or b"{}").get("id", "?")) + f") to {s['to']}"
    msg = EmailMessage()
    msg["From"] = s["from"] or s["smtp_user"] or s["to"]
    msg["To"] = s["to"]
    msg["Subject"] = subject
    msg.set_content(body)
    port = int(s["smtp_port"])
    conn = smtplib.SMTP_SSL(s["smtp_host"], port, timeout=20) if port == 465 else smtplib.SMTP(s["smtp_host"], port, timeout=20)
    with conn:
        if port != 465:
            conn.starttls()
        if s["smtp_user"] and s["smtp_password"]:
            conn.login(s["smtp_user"], s["smtp_password"])
        conn.send_message(msg)
    return f"Sent via {s['smtp_host']} to {s['to']}"


TOOLS = [
    {"name": "email_status",
     "description": "Show how build-questions sends email (method, recipient, missing options). Never shows secrets.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "send_email",
     "description": "Send one plain-text email to the address set in the build-questions plugin options "
                    "(change-request notices and scheduled digests). The recipient is fixed by the options.",
     "inputSchema": {"type": "object", "required": ["subject", "body"], "properties": {
         "subject": {"type": "string", "maxLength": 300},
         "body": {"type": "string", "maxLength": 100000}}}},
]


def handle(req: dict) -> dict | None:
    method, rid = req.get("method"), req.get("id")
    if rid is None:  # notification
        return None
    if method == "initialize":
        result = {"protocolVersion": (req.get("params") or {}).get("protocolVersion") or "2025-06-18",
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "build-questions-mail", "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = req.get("params") or {}
        args = p.get("arguments") or {}
        try:
            if p.get("name") == "email_status":
                text, err = json.dumps(status()), False
            elif p.get("name") == "send_email":
                text, err = send(str(args.get("subject", ""))[:300], str(args.get("body", ""))), False
            else:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "unknown tool"}}
        except Exception as e:  # report, don't crash the server
            text, err = f"Email not sent: {e}", True
        result = {"content": [{"type": "text", "text": text}], "isError": err}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main() -> None:
    # MCP messages are UTF-8 JSON lines on every platform (Windows defaults to a legacy code page).
    stdin = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except (AttributeError, ValueError):
        pass
    for line in stdin:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
