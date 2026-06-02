"""anna_web — Phase 2.5 localhost-only FastAPI dashboard.

A separate user systemd unit (``anna-web.service``) running a small
FastAPI app that gives the operator form-based editors for
``anna.yaml``, ``.env``, and ``schedules.yaml`` plus a one-button
restart of the main daemon.

The dashboard runs out-of-process from the daemon and never mutates
its in-memory state; edits land on disk and the operator presses
Restart. The auth boundary is ``127.0.0.1`` + filesystem permissions
on ``~/anna/.env``; remote access is the operator's reverse-proxy
problem.

See Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md for the full design.
"""
