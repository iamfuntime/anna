"""Watchdog coroutine.

Per v3 section 1. Loops every ``watchdog.interval_seconds`` (default 300) and
does three things:

1. Per-transport liveness ping. Three consecutive failures triggers restart.
2. SDK session liveness check. Two consecutive failures emits CRITICAL.
3. Worker pool stall detection. Logs (and optionally restarts) stuck workers.

Emits DEBUG per cycle, INFO on recovery, WARNING on transient failures, and
CRITICAL when SDK auth is unrecoverable.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from anna.config import AnnaConfig
from anna.log import audit_event, get_logger
from anna.transports.base import ChannelAdapter


class Watchdog:
    def __init__(
        self,
        *,
        config: AnnaConfig,
        adapters: dict[str, ChannelAdapter],
        router: Any,
        alerter: Any | None = None,
    ) -> None:
        self._config = config
        self._adapters = adapters
        self._router = router
        self._alerter = alerter
        self._log = get_logger("anna.watchdog")
        self._transport_failures: dict[str, int] = defaultdict(int)
        self._sdk_failures = 0

    async def run(self) -> None:
        """Loop forever (or until cancelled). One cycle per interval."""
        interval = self._config.watchdog.interval_seconds
        self._log.info("watchdog.started", interval_seconds=interval)
        try:
            while True:
                try:
                    await self._cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log.error("watchdog.cycle_failed", error=str(exc))
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            self._log.info("watchdog.stopped")
            raise

    async def _cycle(self) -> None:
        await self._check_transports()
        await self._check_sdk_session()
        await self._check_worker_pool()

    # ------------------------------------------------------------------
    # Transport health
    # ------------------------------------------------------------------

    async def _check_transports(self) -> None:
        for name, adapter in self._adapters.items():
            self._log.debug("watchdog.transport.ping", transport=name)
            try:
                healthy = await asyncio.wait_for(adapter.health_check(), timeout=10)
            except (asyncio.TimeoutError, Exception) as exc:
                healthy = False
                self._log.warning(
                    "watchdog.transport.ping_failed",
                    transport=name,
                    error=str(exc),
                )
            if healthy:
                if self._transport_failures[name] > 0:
                    downtime = self._transport_failures[name] * self._config.watchdog.interval_seconds
                    self._log.info(
                        "watchdog.transport.recovered",
                        transport=name,
                        prior_failures=self._transport_failures[name],
                    )
                    audit_event(
                        "audit.watchdog.transport_recovered",
                        audit_dir=self._config.audit_dir,
                        actor="watchdog",
                        fsync_on_write=self._config.logging.audit.fsync_on_write,
                        channel=name,
                        downtime_seconds=downtime,
                    )
                self._transport_failures[name] = 0
            else:
                self._transport_failures[name] += 1
                if self._transport_failures[name] >= 3:
                    self._log.warning(
                        "watchdog.transport.restart",
                        transport=name,
                        consecutive_failures=self._transport_failures[name],
                    )
                    audit_event(
                        "audit.watchdog.transport_failed",
                        audit_dir=self._config.audit_dir,
                        actor="watchdog",
                        fsync_on_write=self._config.logging.audit.fsync_on_write,
                        level="WARNING",
                        channel=name,
                        consecutive_failures=self._transport_failures[name],
                    )
                    try:
                        await adapter.restart()
                    except Exception as exc:
                        self._log.error(
                            "watchdog.transport.restart_failed",
                            transport=name,
                            error=str(exc),
                        )
                    # Notify the operator on the other transport so they
                    # know one channel hiccuped even if it self-healed.
                    if self._alerter is not None:
                        try:
                            await self._alerter.warn(
                                f"Transport {name} was restarted after 3 consecutive failed pings.",
                                exclude_channel=name,
                            )
                        except Exception as exc:
                            self._log.error(
                                "watchdog.alerter.warn_failed",
                                transport=name,
                                error=str(exc),
                            )
                    self._transport_failures[name] = 0

    # ------------------------------------------------------------------
    # SDK session
    # ------------------------------------------------------------------

    async def _check_sdk_session(self) -> None:
        """No-op heartbeat against a dedicated ClaudeSDKClient.

        In Phase 1 the heartbeat is a soft check: we instantiate a client and
        confirm it constructs without error. A real query loop would burn
        tokens on every cycle and is deferred to Phase 2.
        """
        self._log.debug("watchdog.sdk.ping")
        try:
            from claude_agent_sdk import ClaudeSDKClient  # noqa: F401
        except ImportError as exc:
            # The SDK is a hard dependency; missing it means the install is
            # broken, not that the session expired.
            self._sdk_failures += 1
            self._log.critical("watchdog.sdk.import_failed", error=str(exc))
            return

        # Construct a client without connecting. The SDK lazily connects on
        # the first ``query()`` call so this is a cheap availability check.
        try:
            ClaudeSDKClient()
            if self._sdk_failures > 0:
                self._log.info("watchdog.sdk.recovered", prior_failures=self._sdk_failures)
            self._sdk_failures = 0
        except Exception as exc:
            self._sdk_failures += 1
            if self._sdk_failures >= 2:
                self._log.critical(
                    "watchdog.sdk.auth_failed",
                    auth_path=self._config.auth.mode,
                    last_error=str(exc),
                )
                audit_event(
                    "audit.watchdog.sdk_unrecoverable",
                    audit_dir=self._config.audit_dir,
                    actor="watchdog",
                    fsync_on_write=self._config.logging.audit.fsync_on_write,
                    level="CRITICAL",
                    auth_path=self._config.auth.mode,
                    last_error=str(exc),
                )
                if self._alerter is not None:
                    try:
                        await self._alerter.critical(
                            f"SDK auth failed (mode={self._config.auth.mode}): {exc}"
                        )
                    except Exception as alert_exc:
                        self._log.error(
                            "watchdog.alerter.critical_failed",
                            error=str(alert_exc),
                        )
            else:
                self._log.warning("watchdog.sdk.ping_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Worker pool
    # ------------------------------------------------------------------

    async def _check_worker_pool(self) -> None:
        try:
            workers = self._router.list_workers()  # type: ignore[attr-defined]
        except AttributeError:
            return

        stall_threshold = self._config.watchdog.worker_stall_seconds
        now = datetime.now(timezone.utc)
        stalled = []
        for worker in workers:
            last_recv = getattr(worker, "last_event_received_at", None)
            last_proc = getattr(worker, "last_event_processed_at", None)
            if last_recv is None or last_proc is None:
                continue
            if (last_recv > last_proc) and (now - last_recv).total_seconds() > stall_threshold:
                stalled.append(worker)

        self._log.debug(
            "watchdog.workers.scan",
            live=len(workers),
            stalled=len(stalled),
        )

        for worker in stalled:
            self._log.warning(
                "watchdog.worker.stalled",
                conv_key=getattr(worker, "conversation_key", "?"),
                stall_seconds=stall_threshold,
            )
            if self._config.watchdog.restart_stalled_workers:
                try:
                    await worker.restart()
                except Exception as exc:
                    self._log.error(
                        "watchdog.worker.restart_failed",
                        conv_key=getattr(worker, "conversation_key", "?"),
                        error=str(exc),
                    )
