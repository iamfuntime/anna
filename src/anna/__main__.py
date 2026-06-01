"""ANNA process entrypoint.

Invoked as ``python -m anna`` or via the ``anna`` console script. Loads
config, wires logging, starts every enabled transport adapter, hands events
to the conversation router, and runs the watchdog and housekeeping coroutines
in parallel until the process is signalled.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Any

from anna.config import AnnaConfig, load_config
from anna.log import configure_logging, get_logger
from anna.runtime.alerter import AdminAlerter
from anna.runtime.router import ConversationRouter
from anna.runtime.schedule_store import ScheduleStore
from anna.runtime.scheduler import Scheduler
from anna.runtime.startup import (
    build_startup_message,
    read_and_clear_sentinel,
    write_clean_shutdown_sentinel,
)
from anna.runtime.supervisor import Supervisor
from anna.runtime.watchdog import Watchdog
from anna.tools.google_clients import GoogleClients
from anna.transports import build_enabled_adapters


# Seconds to wait after kicking listener tasks before firing the
# startup alert. Slack/Telegram adapters need a moment for their
# socket / polling client to attach before ``health_check()`` returns
# True. 3s is comfortably above the ~0.5s observed in production logs.
_STARTUP_ALERT_DELAY_SECONDS = 3.0


async def _run(config: AnnaConfig) -> None:
    """Inner coroutine that owns the asyncio loop."""
    log = get_logger("anna.main")
    log.info("anna.boot", version="0.1.0", auth_mode=config.auth.mode)

    supervisor = Supervisor(config=config)
    adapters = build_enabled_adapters(config)

    # Phase 2 scheduler: build the store and load any persisted schedules
    # before the router so workers see a populated store from the first
    # MCP-tool invocation. The Scheduler itself launches further down with
    # the other aux coroutines.
    schedule_store: ScheduleStore | None = None
    if config.scheduler.enabled:
        schedule_store = ScheduleStore(config=config, supervisor=supervisor)
        try:
            await schedule_store.load()
        except Exception as exc:
            log.error("anna.scheduler.load_failed", error=str(exc))
            log.warning(
                "anna.scheduler.disabled",
                note="failed to load schedules.yaml; running without the scheduler",
            )
            schedule_store = None

    # Phase 2 Google integration. The clients factory is cheap to
    # construct even when no accounts are configured; the actual
    # credential I/O is deferred until a tool is invoked. We still
    # gate on google.enabled so a misconfigured anna.yaml doesn't get
    # the MCP server mounted on a worker that can't use it.
    google_clients: GoogleClients | None = None
    if config.google.enabled:
        google_clients = GoogleClients(config=config)
        log.info(
            "anna.google.ready",
            account_count=len(config.google.accounts),
            slugs=[a.slug for a in config.google.accounts],
        )
    elif config.google.accounts:
        log.warning(
            "anna.google.accounts_without_enabled",
            note=(
                "google.accounts is non-empty but google.enabled is false; "
                "the google MCP server will not be mounted until you flip "
                "the toggle and restart"
            ),
            account_count=len(config.google.accounts),
        )

    router = ConversationRouter(
        config=config,
        supervisor=supervisor,
        adapters=adapters,
        schedule_store=schedule_store,
        google_clients=google_clients,
    )
    alerter = AdminAlerter(config=config, adapters=adapters)
    watchdog = Watchdog(config=config, adapters=adapters, router=router, alerter=alerter)

    # Subscribe the router to every transport. Each adapter calls the
    # handler back with normalized InboundEvent objects.
    for adapter in adapters.values():
        adapter.subscribe(router.dispatch)

    # Read the clean-shutdown sentinel BEFORE we start anything that might
    # fail; if we crash during boot we still want the next boot's alert to
    # report unclean. The sentinel is consumed on read.
    last_clean = read_and_clear_sentinel(config.state_dir)
    boot_time = datetime.now(timezone.utc)

    # Start every adapter listener concurrently. Each lives in its own task
    # so a Slack outage cannot block Telegram.
    listener_tasks = [asyncio.create_task(a.start(), name=f"listener.{a.name}") for a in adapters.values()]
    watchdog_task = asyncio.create_task(watchdog.run(), name="watchdog")
    housekeeping_task = asyncio.create_task(router.run_housekeeping(), name="housekeeping")

    scheduler_task: asyncio.Task[Any] | None = None
    if schedule_store is not None:
        scheduler = Scheduler(
            config=config,
            store=schedule_store,
            router=router,
            adapters=adapters,
            alerter=alerter,
        )
        scheduler_task = asyncio.create_task(scheduler.run(), name="scheduler")

    # Schedule the operator startup ping. It waits a few seconds for
    # adapters to attach, then fires once. We use a background task so
    # the main coroutine can proceed straight to the signal wait.
    startup_alert_task: asyncio.Task[Any] | None = None
    if config.admin.startup_alert:
        startup_alert_task = asyncio.create_task(
            _send_startup_alert(
                alerter=alerter,
                last_clean=last_clean,
                boot_time=boot_time,
            ),
            name="startup_alert",
        )
    else:
        log.info(
            "anna.startup_alert.disabled",
            note="admin.startup_alert is false in anna.yaml",
        )

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
    aux_tasks: list[asyncio.Task[Any]] = [*listener_tasks, watchdog_task, housekeeping_task]
    if startup_alert_task is not None:
        aux_tasks.append(startup_alert_task)
    if scheduler_task is not None:
        aux_tasks.append(scheduler_task)
    for task in aux_tasks:
        task.cancel()

    # Let listener / watchdog / housekeeping cancellation settle before we
    # tear down per-conversation workers. We don't want a still-running
    # dispatch to revive a worker we just stopped.
    await asyncio.gather(*aux_tasks, return_exceptions=True)

    # Close every active conversation worker. Each worker's stop() runs the
    # closeout sequence (checkpoint write + per-core-file eviction), so this
    # is what gives the next boot something to resume from.
    try:
        await router.shutdown()
    except Exception as exc:
        log.warning("anna.shutdown.router_failed", error=str(exc))

    for adapter in adapters.values():
        try:
            await adapter.stop()
        except Exception as exc:
            log.warning("anna.shutdown.adapter_stop_failed", adapter=adapter.name, error=str(exc))

    # Write the clean-shutdown sentinel last. If anything above raised, we
    # skip the sentinel write so the next boot's startup alert correctly
    # reports the prior shutdown as unclean.
    try:
        write_clean_shutdown_sentinel(config.state_dir)
    except OSError as exc:
        log.warning("anna.shutdown.sentinel_write_failed", error=str(exc))

    log.info("anna.shutdown.complete")


async def _send_startup_alert(
    *,
    alerter: AdminAlerter,
    last_clean: datetime | None,
    boot_time: datetime,
) -> None:
    """Wait briefly for adapters to attach, then fire the startup ping."""
    log = get_logger("anna.main")
    try:
        await asyncio.sleep(_STARTUP_ALERT_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    message = build_startup_message(
        last_clean_shutdown=last_clean,
        boot_time=boot_time,
        pid=os.getpid(),
    )
    try:
        delivered = await alerter.notify_startup(message)
    except Exception as exc:
        log.error("anna.startup_alert.failed", error=str(exc))
        return
    if delivered:
        log.info(
            "anna.startup_alert.sent",
            clean=last_clean is not None,
        )
    else:
        log.warning(
            "anna.startup_alert.undeliverable",
            note="no surviving transport with an admin destination",
        )


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
