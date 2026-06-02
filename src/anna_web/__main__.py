"""ANNA Web Dashboard entry point.

Loads anna.yaml via ``anna.config.load_config``, reads the ``web:``
block, and starts uvicorn on the configured host/port. If
``web.enabled`` is false, log a startup-disabled line and exit 0
cleanly so the systemd unit can be permanently enabled but
operationally opted out.

Logging follows the same structlog setup as ``anna.__main__`` so the
operator sees one coherent JSON stream when both units are running
under journald.
"""

from __future__ import annotations

import sys

from anna.config import _resolve_config_path, load_config
from anna.log import configure_logging, get_logger
from anna_web import audit as web_audit


def main() -> int:
    try:
        cfg = load_config()
    except Exception as exc:
        # Logging is not configured yet at this point. Mirror the
        # daemon's behavior: write to stderr and exit non-zero so
        # systemd restarts us.
        sys.stderr.write(f"anna-web: failed to load config: {exc}\n")
        return 2

    configure_logging(level=cfg.logging.level, format=cfg.logging.format)
    log = get_logger("anna.web")

    config_path = _resolve_config_path()

    if cfg.web.enabled is False:
        log.info(
            "anna.web.dashboard.disabled",
            config_path=str(config_path),
            note=(
                "web.enabled is false in anna.yaml; the dashboard unit "
                "started cleanly but will not bind a port"
            ),
        )
        return 0

    log.info(
        "anna.web.dashboard.ready",
        host=cfg.web.host,
        port=cfg.web.port,
        anna_home=str(cfg.anna_home),
        config_path=str(config_path),
    )

    # Pair the operational log line with an audit row so the operator
    # can reconstruct dashboard boots from the audit JSONL alone. The
    # lifespan handler in app.py owns the matching shutdown emit.
    try:
        web_audit.emit(
            "boot",
            cfg=cfg,
            host=cfg.web.host,
            port=cfg.web.port,
        )
    except Exception:  # pragma: no cover - defensive
        # An audit-write failure must not prevent the dashboard from
        # starting; the operational log line above is still in
        # journald. anna.log.audit_event already mirrors to the
        # operational stream + logs a CRITICAL on disk failure, so
        # this guard exists only for the truly unreachable path
        # (e.g. cfg.audit_dir computed wrong).
        pass

    # Import lazily so a disabled-mode start does not pay the FastAPI
    # import cost (and so tests for the disabled branch can monkey-patch
    # load_config without uvicorn ever resolving).
    import uvicorn

    # String-import form matches the FastAPI convention and keeps the
    # door open for --reload semantics in dev runs. log_config=None
    # disables uvicorn's own logging config so structlog stays the
    # single source of formatted output.
    uvicorn.run(
        "anna_web.app:app",
        host=cfg.web.host,
        port=cfg.web.port,
        log_config=None,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
