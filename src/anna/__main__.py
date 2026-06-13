"""ANNA process entrypoint.

Invoked as ``python -m anna`` or via the ``anna`` console script. With no
subcommand the daemon boots (loads config, wires logging, starts every
enabled transport adapter, hands events to the conversation router, and
runs the watchdog and housekeeping coroutines in parallel until the
process is signalled). With a subcommand, dispatches to the matching CLI
client.

Subcommands (Phase 2 §5 subtask 11 — operator decision #6 unified the
entry points under a single ``anna`` console-script):

* ``daemon`` (or no subcommand): run the daemon. Preserves the
  systemd-unit invocation ``ExecStart=%h/anna/.venv/bin/anna``.
* ``chat``: interactive CLI client (``anna.cli.chat:main``).
* ``ask``:  one-shot CLI client (``anna.cli.ask:main``).
* ``admin``: operator administration commands (``anna.cli.admin:main``).

Subcommand modules are imported lazily so a broken ``admin`` module (or
one that does not yet exist) cannot crash ``anna chat`` or ``anna ask``
and cannot regress the daemon-startup path the systemd unit relies on.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Any

from anna.agents.registry import SubAgentRegistry
from anna.auth import ensure_isolated_config_dir
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
from anna.runtime.subagent import SubAgentRunner
from anna.runtime.supervisor import Supervisor
from anna.runtime.voice import build_voice_processor
from anna.runtime.watchdog import Watchdog
from anna.skills.registry import SkillRegistry
from anna.tools.google_clients import GoogleClients
from anna.tools.web_server import WEB_TOOL_NAMES
from anna.transports import build_enabled_adapters


# Seconds to wait after kicking listener tasks before firing the
# startup alert. Slack/Telegram adapters need a moment for their
# socket / polling client to attach before ``health_check()`` returns
# True. 3s is comfortably above the ~0.5s observed in production logs.
_STARTUP_ALERT_DELAY_SECONDS = 3.0

# Subcommands the dispatcher routes outside of the daemon path. The
# value is the dotted module path that exposes a ``main()`` callable.
# Lazy-imported only when the operator actually invokes that subcommand.
_CLI_SUBCOMMANDS: dict[str, str] = {
    "chat": "anna.cli.chat",
    "ask": "anna.cli.ask",
    "admin": "anna.cli.admin",
}


async def _run(config: AnnaConfig) -> None:
    """Inner coroutine that owns the asyncio loop."""
    log = get_logger("anna.main")
    log.info("anna.boot", version="0.1.0", auth_mode=config.auth.mode)

    supervisor = Supervisor(config=config)

    # Isolate the spawned CLI subprocesses' CLAUDE_CONFIG_DIR before any worker
    # spins up. The bundled Claude CLI discovers host CLAUDE.md / skills /
    # plugins / local MCP from CLAUDE_CONFIG_DIR (defaults to $HOME/.claude);
    # the daemon inherits the operator's HOME, so without this every worker
    # leaks the operator's entire Claude Code environment. Credentials are NOT
    # seeded into this dir — workers point the CLI at the operator's real
    # ~/.claude via CLAUDE_SECURESTORAGE_CONFIG_DIR (in max mode) so OAuth reads
    # and the refresh-write share the operator's .credentials.json directly.
    runtime_dir = ensure_isolated_config_dir(
        config.claude_runtime_dir, config.auth.mode
    )
    securestorage_dir = config.claude_securestorage_dir
    log.info(
        "anna.claude_runtime.ready",
        dir=str(runtime_dir),
        securestorage_dir=str(securestorage_dir),
        credentials_present=(securestorage_dir / ".credentials.json").exists(),
    )

    # Defensive cwd-walk scan. Even with CLAUDE_CONFIG_DIR relocated, the CLI
    # still walks the cwd (vault root) and its parents for a stray .claude/
    # dir, a CLAUDE.md (other than ANNA's own core/CLAUDE.md), or a .mcp.json.
    # Any of those would be discovered and leak in. We log a WARNING per
    # finding rather than fail boot — the operator decides whether to remove
    # them.
    _scan_for_stray_claude_artifacts(config, log)

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

    # Phase 2 §2 tool surface. Logged here for parity with the google block
    # so the operator sees one line per MCP server at bootstrap. The actual
    # mount happens in worker._build_options when each worker spins up.
    if config.tools.enabled:
        log.info(
            "anna.web.ready",
            tools=list(WEB_TOOL_NAMES),
            web_search_provider=config.tools.web_search.provider,
            vault_download_destination=str(
                config.tools.vault_download.resolved_destination
            ),
        )
    else:
        log.info(
            "anna.web.disabled",
            note=(
                "tools.enabled is false; the anna_web MCP server will not be "
                "mounted on workers"
            ),
        )

    # Phase 2 §3 sub-agent runtime. One SubAgentRunner per process so the
    # concurrency semaphore is system-wide rather than per-worker. The
    # runner is pull-driven by delegate calls; no aux task to launch. We
    # construct the registries the runner reads off disk (persona + skill
    # files) right here so the runner has stable references that match
    # the live tree's layout.
    subagent_runner: SubAgentRunner | None = None
    if config.subagents.enabled:
        subagent_agents_registry = SubAgentRegistry(
            supervisor=supervisor,
            agents_dir=config.anna_home / "agents",
            audit_dir=config.audit_dir,
            fsync_on_write=config.logging.audit.fsync_on_write,
        )
        subagent_skills_registry = SkillRegistry(
            supervisor=supervisor,
            skills_dir=config.anna_home / "skills",
            audit_dir=config.audit_dir,
            fsync_on_write=config.logging.audit.fsync_on_write,
        )
        subagent_runner = SubAgentRunner(
            config=config,
            supervisor=supervisor,
            agents_registry=subagent_agents_registry,
            skills_registry=subagent_skills_registry,
        )
        log.info(
            "anna.subagent.ready",
            max_concurrent=config.subagents.max_concurrent,
            allowed_tools_count=len(config.subagents.allowed_tools),
            default_timeout_seconds=config.subagents.default_timeout_seconds,
        )
    else:
        log.info(
            "anna.subagent.disabled",
            note=(
                "subagents.enabled is false; the anna_delegate MCP server "
                "will not be mounted on workers"
            ),
        )

    # Phase 2.5 voice messages. One process-wide VoiceProcessor, constructed
    # after the sub-agent runner and before the router, then threaded into the
    # Slack and Telegram adapters via build_enabled_adapters. The factory is
    # boot-safe: if a configured provider's API key is missing it degrades that
    # provider to None and warns rather than raising, so a misconfigured voice
    # block can never crash daemon startup. We always construct it (it is cheap
    # — no I/O at build time) and emit a single ready/disabled line mirroring
    # anna.web.ready / anna.subagent.ready / anna.cli.ready. The adapters guard
    # all voice logic on a non-None provider, so an enabled block with no key
    # still routes inbound voice through the polite "transcription is off" path.
    voice_processor = build_voice_processor(config)
    if config.voice.inbound.enabled or config.voice.outbound.enabled:
        log.info(
            "anna.voice.ready",
            inbound_enabled=config.voice.inbound.enabled,
            inbound_provider=(
                config.voice.inbound.provider
                if config.voice.inbound.enabled
                else None
            ),
            outbound_enabled=config.voice.outbound.enabled,
            outbound_provider=(
                config.voice.outbound.provider
                if config.voice.outbound.enabled
                else None
            ),
            outbound_transports=(
                list(config.voice.outbound.transports)
                if config.voice.outbound.enabled
                else []
            ),
            retention_days=config.logging.transcripts.retention_days,
        )
    else:
        log.info(
            "anna.voice.disabled",
            note=(
                "voice.inbound.enabled and voice.outbound.enabled are both "
                "false; inbound voice notes are ignored and replies stay "
                "text-only on every transport"
            ),
        )

    # Build the transport adapters now that the VoiceProcessor exists, so the
    # Slack and Telegram adapters receive it. Constructed after voice / before
    # the router (the first consumer of `adapters`).
    adapters = build_enabled_adapters(config, voice=voice_processor)

    # Cadence-Visibility hooks (per Inbox/2026-06-02 plan, subtask 12).
    # The actual wiring lives in ConversationRouter._build_visibility_callbacks
    # — this block just emits the boot-time visibility line that mirrors
    # anna.web.ready / anna.subagent.ready / anna.cli.ready. The "ready"
    # branch fires when ANY of the three flags is on (the surface is at
    # least partially live); the "disabled" branch fires only when ALL
    # three are off so the operator sees one explicit "feature is fully
    # turned off" line at boot.
    if (
        config.runtime.visibility.reaction_signal
        or config.runtime.visibility.cadence_reminder
        or config.runtime.visibility.response_lint
    ):
        log.info(
            "anna.visibility.ready",
            reaction_signal=config.runtime.visibility.reaction_signal,
            cadence_reminder=config.runtime.visibility.cadence_reminder,
            response_lint=config.runtime.visibility.response_lint,
            slack_emoji=config.runtime.visibility.slack_emoji,
            telegram_typing_max_seconds=config.runtime.visibility.telegram_typing_max_seconds,
        )
    else:
        log.info(
            "anna.visibility.disabled",
            note=(
                "runtime.visibility.{reaction_signal,cadence_reminder,"
                "response_lint} are all false; cadence-visibility hooks "
                "are fully off — buffered transports will resume the "
                "pre-Phase-2 10-90s blank-pause behavior"
            ),
        )

    # Phase 2 §5 CLI transport. The adapter itself is constructed inside
    # build_enabled_adapters and started with the other listener tasks
    # below; this block just emits the boot-time visibility line that
    # mirrors anna.web.ready / anna.subagent.ready.
    if config.transports.cli.enabled:
        log.info(
            "anna.cli.ready",
            socket_path=str(config.transports.cli.resolved_socket_path),
            idle_gap_minutes=config.transports.cli.idle_gap_minutes,
            identities=[i.canonical for i in config.identities],
        )
    else:
        log.info(
            "anna.cli.disabled",
            note=(
                "transports.cli.enabled is false; the CLI socket will not "
                "be bound and `anna chat`/`anna ask` will not have anything "
                "to connect to"
            ),
        )

    # Build the alerter BEFORE the router so it can be threaded into every
    # worker the router spawns (the tool-call-markup guard fires a
    # best-effort admin alert through it).
    alerter = AdminAlerter(config=config, adapters=adapters)
    router = ConversationRouter(
        config=config,
        supervisor=supervisor,
        adapters=adapters,
        schedule_store=schedule_store,
        google_clients=google_clients,
        subagent_runner=subagent_runner,
        alerter=alerter,
    )
    # Hand the alerter back to every adapter (setter injection — the
    # alerter itself needs the adapters dict at construction) so a
    # transport that boots without its token can warn the operator on a
    # surviving channel instead of failing quietly before the auth
    # handshake.
    for adapter in adapters.values():
        adapter.set_alerter(alerter)
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
        # Deleting a schedule must also cancel its queued/in-flight fire
        # tasks (2026-06-01 incident: leftover queued fires failed loudly
        # after the schedule was deleted). The store-level callback covers
        # every in-process delete path — notably the MCP schedule_delete
        # tool, which only holds the store.
        schedule_store.set_cancel_callback(scheduler.cancel_schedule_tasks)
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


def _scan_for_stray_claude_artifacts(config: AnnaConfig, log: Any) -> None:
    """Warn on cwd-walk-discoverable Claude artifacts near the vault root.

    The CLI's cwd is the vault root (see worker._build_options). The bundled
    CLI walks the cwd and its ancestors looking for a ``.claude/`` dir, a
    ``CLAUDE.md``, or a ``.mcp.json`` — all of which it would discover and
    fold into a worker session even with CLAUDE_CONFIG_DIR relocated. ANNA's
    own ``core/CLAUDE.md`` is the one legitimate CLAUDE.md and is excluded.

    This is telemetry only: each finding is a WARNING and boot continues. The
    operator decides whether to relocate or remove the artifact.
    """
    vault_root = config.vault.resolved_path
    core_claude_md = (config.core_dir / "CLAUDE.md").resolve()

    # The cwd-walk set: the vault root plus each ancestor up to (and
    # including) the filesystem root.
    scan_dirs: list[Path] = []
    cur = vault_root
    while True:
        scan_dirs.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent

    seen: set[Path] = set()
    for d in scan_dirs:
        for name in (".claude", "CLAUDE.md", ".mcp.json"):
            candidate = d / name
            try:
                if not candidate.exists():
                    continue
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if name == "CLAUDE.md" and resolved == core_claude_md:
                # ANNA's own identity file — expected, not a leak.
                continue
            log.warning(
                "anna.claude_runtime.stray_artifact",
                path=str(candidate),
                artifact=name,
                note=(
                    "cwd-walk discoverable by the bundled CLI; may leak host "
                    "Claude Code context into worker sessions"
                ),
            )


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


def run_daemon() -> int:
    """Boot the ANNA daemon: load config, wire logging, run the loop.

    Extracted verbatim from the pre-dispatcher ``main()`` body so the
    systemd unit (``ExecStart=%h/anna/.venv/bin/anna`` — no subcommand)
    keeps working unchanged. The dispatcher routes both the
    no-subcommand and explicit ``daemon`` subcommand cases here.
    """
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


def _build_parser() -> argparse.ArgumentParser:
    """Construct the outer argparse parser for ``anna --help`` output.

    Only used to render help text and to validate the first positional
    argument against the known-subcommand list. We do NOT use this
    parser to slice the per-subcommand argument tail — argparse has a
    long-standing quirk where a leading ``--flag`` after a subcommand
    gets consumed by the outer parser even when the subcommand
    declares ``nargs=argparse.REMAINDER`` (see
    https://bugs.python.org/issue9334). Instead, ``main()`` reads
    ``sys.argv`` directly once it knows the subcommand is valid; the
    parser here is purely for the help banner and for the unknown-
    subcommand error path.
    """
    parser = argparse.ArgumentParser(
        prog="anna",
        description=(
            "ANNA: Adaptive Neural Network Assistant. With no subcommand "
            "the daemon boots (the systemd-unit invocation path). With a "
            "subcommand, dispatches to the matching client."
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=False)
    subparsers.add_parser(
        "daemon",
        help="run the ANNA daemon (default when no subcommand is given)",
        add_help=False,
    )
    for name, module_path in _CLI_SUBCOMMANDS.items():
        subparsers.add_parser(
            name,
            help=f"dispatch to {module_path}:main",
            add_help=False,  # let the subcommand show its own --help
        )
    return parser


def _dispatch_subcommand(name: str, rest: list[str]) -> int:
    """Lazy-import the subcommand module and invoke its ``main()``.

    Rewrites ``sys.argv`` to ``["anna <name>", *rest]`` before calling
    so the subcommand's argv-parsing (whether argparse, click, or
    hand-rolled) sees the right shape. Restores ``sys.argv`` on the way
    out so test runners that re-enter the dispatcher in the same
    process don't see leaked state.

    For ``admin``: if the module fails to import we surface the stub
    message and exit 2 (per subtask 11 spec — keeps the dispatcher
    robust if a later refactor breaks ``anna.cli.admin``).
    """
    log = get_logger("anna.main")
    log.debug("anna.dispatch", chosen=name)

    module_path = _CLI_SUBCOMMANDS[name]
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        if name == "admin":
            sys.stderr.write(
                "anna admin subcommands not yet available — pending subtask 12\n"
            )
            return 2
        sys.stderr.write(f"anna {name}: failed to import {module_path}: {exc}\n")
        return 2

    if module is None or not hasattr(module, "main"):
        if name == "admin":
            sys.stderr.write(
                "anna admin subcommands not yet available — pending subtask 12\n"
            )
            return 2
        sys.stderr.write(
            f"anna {name}: module {module_path} has no main() entry point\n"
        )
        return 2

    saved_argv = sys.argv
    sys.argv = [f"anna {name}", *rest]
    try:
        result = module.main()
    except SystemExit as exc:
        # click's standalone_mode=True (the default) raises SystemExit
        # from inside main(). Surface its code so the outer process
        # exit reflects the subcommand's intent.
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        return code
    finally:
        sys.argv = saved_argv

    if result is None:
        return 0
    if isinstance(result, int):
        return result
    return 0


def main() -> int:
    """``anna`` console entrypoint and ``python -m anna`` dispatcher.

    No subcommand → daemon (the systemd-unit case). Explicit ``daemon``
    → also the daemon. ``chat`` / ``ask`` / ``admin`` → lazy-import and
    forward. Anything else → usage to stderr, exit 2.

    The no-subcommand → daemon shortcut is taken via direct ``sys.argv``
    inspection *before* argparse runs (per operator decision #6 — the
    pre-existing systemd unit invokes ``anna`` with no args and must
    keep working without argparse printing a help banner and exiting).

    For the subcommand arms we slice ``sys.argv`` by hand rather than
    rely on ``argparse.REMAINDER``: REMAINDER drops leading ``--flag``
    tokens at the outer level (Python bug #9334), which would break
    ``anna ask --foo``. The shape we want — "first positional decides
    the subcommand, everything after that is passed through verbatim"
    — is trivial without argparse once we've already detected the
    no-arg fast-path.
    """
    # Fast-path: bare ``anna`` (no args) → daemon. Done before argparse
    # so the help-on-no-subcommand default is never triggered. See the
    # subtask 11 spec ("Decisions section") for the choice rationale.
    if len(sys.argv) == 1:
        return run_daemon()

    # Handle top-level help / version before subcommand routing so
    # ``anna --help`` works the way an operator expects (and doesn't
    # get parsed as a subcommand).
    first = sys.argv[1]
    if first in ("-h", "--help"):
        _build_parser().print_help()
        return 0

    if first == "daemon":
        # ``anna daemon`` with trailing args is treated the same as
        # bare ``anna daemon`` — the daemon does not accept argv flags
        # today; if a future slice grows them, swap to argparse here.
        return run_daemon()

    if first in _CLI_SUBCOMMANDS:
        rest = list(sys.argv[2:])
        return _dispatch_subcommand(first, rest)

    # Anything else: print usage to stderr and exit 2. We synthesize a
    # short error line first so the operator sees the bad-token name,
    # then dump the help banner from the parser.
    sys.stderr.write(f"anna: unknown subcommand: {first}\n")
    _build_parser().print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
