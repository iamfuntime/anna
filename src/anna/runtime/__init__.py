"""Runtime: supervisor, watchdog, router, per-conversation worker.

The runtime is the part of ANNA that owns the asyncio loop and coordinates
all other subsystems. See v3 sections 1 and 6 for the architecture.
"""
