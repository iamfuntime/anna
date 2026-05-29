"""ANNA process entrypoint.

Invoked as ``python -m anna`` or via the ``anna`` console script. Loads
config, wires logging, starts every enabled transport adapter, hands events
to the conversation router, and runs the watchdog and housekeeping coroutines
in parallel until the process is signalled.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

from anna.config import AnnaConfig, load_config
from anna.log import configure_logging, get_logger
from anna.runtime.router import ConversationRouter
from anna.runtime.supervisor import Supervisor
from anna.runtime.watchdog import Watchdog
from anna.transports import build_enabled_adapters


async def _run(config: AnnaConfig) -> None:
    """Inner coroutine that owns the asyncio loop."""
    log = get_logger("anna.main")
    log.info("anna.boot", version="0.1.0", auth_mode=config.auth.mode)

    supervisor = Supervisor(config=config)
    adapters = build_enabled_adapters(config)
    router = ConversationRouter(config=config, supervisor=supervisor, adapters=adapters)
    watchdog = Watchdog(config=config, adapters=adapters, router=router)

    # Subscribe the router to every transport. Each adapter calls the
    # handler back with normalized InboundEvent objects.
    for adapter in adapters.values():
        adapter.subscribe(router.dispatch)

    # Start every adapter listener concurrently. Each lives in its own task
    # so a Slack outage cannot block Telegram.
    listener_tasks = [asyncio.create_task(a.start(), name=f"listener.{a.name}") for a in adapters.values()]
    watchdog_task = asyncio.create_task(watchdog.run(), name="watchdog")
    housekeeping_task = asyncio.create_task(router.run_housekeeping(), name="housekeeping")

    stop_event = asyncio.Event()

    def _handle_signal(*_args: Any) -> None:
        log.info("anna.shutdown.signal")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Signal handlers are unavailable on some non-Unix platforms.
            pass

    await stop_event.wait()

    log.info("anna.shutdown.start")
    for task in (*listener_tasks, watchdog_task, housekeeping_task):
        task.cancel()
    for adapter in adapters.values():
        try:
            await adapter.stop()
        except Exception as exc:
            log.warning("anna.shutdown.adapter_stop_failed", adapter=adapter.name, error=str(exc))

    # Let cancellation propagate cleanly.
    await asyncio.gather(*listener_tasks, watchdog_task, housekeeping_task, return_exceptions=True)
    log.info("anna.shutdown.complete")


def main() -> int:
    """Console entrypoint."""
    try:
        config = load_config()
    except Exception as exc:
        # Logging is not configured yet at this point, so write the failure
        # to stderr and exit with a non-zero code. Systemd will restart us.
        sys.stderr.write(f"anna: failed to load config: {exc}\n")
        return 2

    configure_logging(level=config.logging.level, format=config.logging.format)

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
