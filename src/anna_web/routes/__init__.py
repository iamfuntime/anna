"""Route modules for the ANNA web dashboard.

Each module under ``anna_web.routes`` exposes a FastAPI ``APIRouter``
that the ``create_app`` factory in :mod:`anna_web.app` registers via
``app.include_router``. Splitting routes per resource (config, env,
schedules, restart, healthz) keeps each subtask's diff scoped and
avoids merge collisions when subtasks 7/8/9 land in parallel.
"""
