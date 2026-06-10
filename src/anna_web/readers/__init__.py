"""Read-only data layers for the Mission Control dashboard.

Each module under ``anna_web.readers`` exposes a small synchronous
reader class that route handlers call via ``run_in_threadpool``. The
readers never write, never raise into callers, and bound every read
(byte caps, line caps) so a dashboard poll can never stall on or slurp
an unbounded daemon-owned file.
"""
