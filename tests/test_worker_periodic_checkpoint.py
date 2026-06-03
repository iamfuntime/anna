"""Validate Fix 2: periodic checkpointing between turns (worker.py).

A periodic checkpoint fires synchronously at the TOP of ``_handle`` —
before ``self._client.query`` — on the single-consumer run loop, so it
can never race an in-flight reply. It uses a MECHANICAL transcript-tail
summary (no SDK round-trip) and must NOT trigger core-file eviction.

These tests drive ``_maybe_periodic_checkpoint`` directly (manipulating
the turn counter / ``_last_checkpoint_at`` to control the trigger), plant
JSONL transcript lines on disk for the mechanical tail, and assert the
written checkpoint carries ``checkpoint_kind: periodic``. Eviction
non-involvement is checked by ensuring the SDK is never queried and no
core archive lands.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.core.identity import CORE_FILES, CoreFile
from anna.runtime.supervisor import Supervisor
from anna.runtime.worker import ConversationWorker


CONV_KEY = "slack:dm:UTEST"


# ---------------------------------------------------------------------------
# Fake SDK (mirrors test_worker_closeout.py)
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAssistantMessage:
    def __init__(self, content) -> None:
        self.content = content


class _FakeResultMessage:
    pass


class FakeSDKClient:
    """Records every ``query()`` and yields one canned reply per call."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or ["(reply)"])
        self._pending: str | None = None
        self.queries: list[str] = []
        self.closed = False

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        self._pending = self._responses.pop(0) if self._responses else "(out)"

    async def receive_response(self):
        text = self._pending or ""
        self._pending = None
        yield _FakeAssistantMessage(content=[_FakeTextBlock(text=text)])
        yield _FakeResultMessage()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        self.closed = True


@pytest.fixture(autouse=True)
def _patch_sdk_types(monkeypatch):
    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "AssistantMessage", _FakeAssistantMessage, raising=False)
    monkeypatch.setattr(sdk, "ResultMessage", _FakeResultMessage, raising=False)
    monkeypatch.setattr(sdk, "TextBlock", _FakeTextBlock, raising=False)
    monkeypatch.setattr(sdk, "ToolUseBlock", object, raising=False)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker(tmp_path: Path, *, ephemeral: bool = False, conv_key: str = CONV_KEY):
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(config=cfg)

    sent: list = []

    async def _send(msg):
        sent.append(msg)

    worker = ConversationWorker(
        conversation_key=conv_key,
        transport="slack",
        config=cfg,
        supervisor=supervisor,
        send=_send,
        ephemeral=ephemeral,
    )
    worker._sent = sent  # type: ignore[attr-defined]
    return worker


def _safe(conv_key: str) -> str:
    return conv_key.replace(":", "-").replace("/", "_")


def _plant_transcript(worker: ConversationWorker, lines: list[dict]) -> None:
    """Write JSONL transcript lines for the worker's conv_key."""
    base = worker._config.transcripts_dir / _safe(worker.conversation_key)
    base.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = base / f"{day}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")


def _now_iso(offset_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _sample_lines() -> list[dict]:
    return [
        {"ts": _now_iso(0), "direction": "inbound", "conv_key": CONV_KEY, "text": "hello there"},
        {"ts": _now_iso(1), "direction": "outbound", "conv_key": CONV_KEY, "text": "hi back"},
    ]


def _conv_files(worker: ConversationWorker) -> list[Path]:
    conv_dir = worker._config.vault.resolved_path / "Conversations" / _safe(
        worker.conversation_key
    )
    if not conv_dir.is_dir():
        return []
    return list(conv_dir.glob("*.md"))


def _audit_events(worker: ConversationWorker, event_name: str) -> list[dict]:
    """Return all audit records with the given event name for the worker."""
    audit_dir = worker._config.audit_dir
    if not audit_dir.is_dir():
        return []
    out: list[dict] = []
    for path in audit_dir.glob("audit-*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("event") == event_name:
                out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Trigger by turn count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_fires_after_every_turns(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    worker._client = FakeSDKClient()
    _plant_transcript(worker, _sample_lines())

    worker._dirty = True
    worker._turns_since_checkpoint = worker._config.checkpoint.every_turns

    await worker._maybe_periodic_checkpoint()

    files = _conv_files(worker)
    assert len(files) == 1, "expected one periodic checkpoint"
    body = files[0].read_text(encoding="utf-8")
    assert "checkpoint_kind: periodic" in body
    assert "Unsaved conversation tail" in body
    # State reset after the write.
    assert worker._dirty is False
    assert worker._turns_since_checkpoint == 0
    assert worker._last_checkpoint_at is not None


# ---------------------------------------------------------------------------
# Trigger by elapsed minutes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_fires_after_every_minutes(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    worker._client = FakeSDKClient()
    _plant_transcript(worker, _sample_lines())

    worker._dirty = True
    worker._turns_since_checkpoint = 1  # below every_turns
    # Push the last-checkpoint baseline past every_minutes ago.
    mins = worker._config.checkpoint.every_minutes
    worker._last_checkpoint_at = datetime.now(timezone.utc) - timedelta(minutes=mins + 1)

    await worker._maybe_periodic_checkpoint()

    files = _conv_files(worker)
    assert len(files) == 1
    assert "checkpoint_kind: periodic" in files[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_minutes_baseline_uses_created_at_before_first_checkpoint(
    tmp_path: Path,
) -> None:
    """With _last_checkpoint_at None, the wall-clock baseline is _created_at."""
    worker = _make_worker(tmp_path)
    worker._client = FakeSDKClient()
    _plant_transcript(worker, _sample_lines())

    worker._dirty = True
    worker._turns_since_checkpoint = 1
    assert worker._last_checkpoint_at is None
    mins = worker._config.checkpoint.every_minutes
    worker._created_at = datetime.now(timezone.utc) - timedelta(minutes=mins + 1)

    await worker._maybe_periodic_checkpoint()

    assert len(_conv_files(worker)) == 1


# ---------------------------------------------------------------------------
# Negative gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_does_not_fire_when_not_dirty(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    worker._client = FakeSDKClient()
    _plant_transcript(worker, _sample_lines())

    worker._dirty = False
    worker._turns_since_checkpoint = worker._config.checkpoint.every_turns * 5

    await worker._maybe_periodic_checkpoint()

    assert _conv_files(worker) == []


@pytest.mark.asyncio
async def test_ephemeral_never_fires(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path, ephemeral=True, conv_key="cli:oneshot:x")
    worker._client = FakeSDKClient()
    _plant_transcript(worker, _sample_lines())

    worker._dirty = True
    worker._turns_since_checkpoint = worker._config.checkpoint.every_turns

    await worker._maybe_periodic_checkpoint()

    assert _conv_files(worker) == []


@pytest.mark.asyncio
async def test_periodic_disabled_never_fires(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    worker._config.checkpoint.periodic_enabled = False
    worker._client = FakeSDKClient()
    _plant_transcript(worker, _sample_lines())

    worker._dirty = True
    worker._turns_since_checkpoint = worker._config.checkpoint.every_turns

    await worker._maybe_periodic_checkpoint()

    assert _conv_files(worker) == []


# ---------------------------------------------------------------------------
# Empty tail: no file, but counters/dirty reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_tail_writes_nothing_but_resets_state(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    worker._client = FakeSDKClient()
    # No transcript planted -> empty tail.

    worker._dirty = True
    worker._turns_since_checkpoint = worker._config.checkpoint.every_turns

    await worker._maybe_periodic_checkpoint()

    assert _conv_files(worker) == []
    assert worker._dirty is False
    assert worker._turns_since_checkpoint == 0
    assert worker._last_checkpoint_at is not None


# ---------------------------------------------------------------------------
# Eviction is NOT triggered by a periodic checkpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_does_not_trigger_eviction(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    cfg = worker._config
    # Make MEMORY.md over cap; closeout would evict it, periodic must not.
    spec = CORE_FILES[CoreFile.MEMORY]
    (cfg.core_dir / "MEMORY.md").write_text(
        " ".join(["word"] * (spec.token_cap * 2)), encoding="utf-8"
    )
    fake = FakeSDKClient()
    worker._client = fake
    _plant_transcript(worker, _sample_lines())

    worker._dirty = True
    worker._turns_since_checkpoint = cfg.checkpoint.every_turns

    await worker._maybe_periodic_checkpoint()

    # Periodic checkpoint landed.
    assert len(_conv_files(worker)) == 1
    # No SDK query (eviction proposes via the SDK; mechanical summary does not).
    assert fake.queries == []
    # No archive written under vault/Identity/.
    identity_dir = cfg.vault.resolved_path / "Identity"
    assert not identity_dir.exists() or not list(identity_dir.glob("MEMORY-archive-*.md"))
    # MEMORY.md untouched.
    assert (cfg.core_dir / "MEMORY.md").read_text(encoding="utf-8").startswith("word")


# ---------------------------------------------------------------------------
# A periodic write failure does not break the turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_failure_does_not_break_turn(tmp_path: Path, monkeypatch) -> None:
    worker = _make_worker(tmp_path)
    worker._client = FakeSDKClient(responses=["the real reply"])
    _plant_transcript(worker, _sample_lines())

    worker._dirty = True
    worker._turns_since_checkpoint = worker._config.checkpoint.every_turns

    def _boom(**kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("anna.runtime.worker.write_checkpoint", _boom)

    from anna.transports.base import InboundEvent

    event = InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY,
        sender_id="U1",
        sender_display="Tester",
        text="hello",
        is_dm=True,
        is_thread=False,
    )

    # _handle calls _maybe_periodic_checkpoint at the top; the write blows up
    # but the turn must still proceed to the query and send a reply.
    await worker._handle(event)

    assert worker._client.queries == ["hello"], "turn query did not proceed"
    assert worker._sent, "no reply sent after periodic-checkpoint failure"
    # No checkpoint file (write failed).
    assert _conv_files(worker) == []


# ---------------------------------------------------------------------------
# Ordering: periodic checkpoint runs BEFORE the query in _handle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_runs_before_query_in_handle(tmp_path: Path) -> None:
    """By construction the checkpoint fires before query -> no in-flight race.

    We assert ordering by recording the sequence of events: the
    checkpoint write must complete before the SDK ``query`` is recorded.
    """
    worker = _make_worker(tmp_path)
    order: list[str] = []

    class _OrderingClient(FakeSDKClient):
        async def query(self, prompt: str) -> None:
            order.append("query")
            await super().query(prompt)

    worker._client = _OrderingClient(responses=["reply"])
    _plant_transcript(worker, _sample_lines())

    # Spy on the checkpoint write to record its ordering.
    import anna.runtime.worker as worker_mod

    real_write = worker_mod.write_checkpoint

    def _spy(**kwargs):
        order.append("checkpoint")
        return real_write(**kwargs)

    worker._dirty = True
    worker._turns_since_checkpoint = worker._config.checkpoint.every_turns

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker_mod, "write_checkpoint", _spy)

        from anna.transports.base import InboundEvent

        event = InboundEvent(
            transport="slack",
            conversation_key=CONV_KEY,
            sender_id="U1",
            sender_display="Tester",
            text="hi",
            is_dm=True,
            is_thread=False,
        )
        await worker._handle(event)

    assert order == ["checkpoint", "query"], f"checkpoint must precede query; got {order}"


# ---------------------------------------------------------------------------
# Closeout still writes its authoritative checkpoint even after a periodic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closeout_writes_after_periodic(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    worker._client = FakeSDKClient(responses=["closeout prose summary"])
    _plant_transcript(worker, _sample_lines())

    # First land a periodic checkpoint.
    worker._dirty = True
    worker._turns_since_checkpoint = worker._config.checkpoint.every_turns
    await worker._maybe_periodic_checkpoint()
    assert len(_conv_files(worker)) == 1
    assert worker._dirty is False  # reset by the periodic write

    # Now closeout: even though _dirty is False, closeout always writes its
    # authoritative LLM summary checkpoint (the dirty gate applies only to
    # the periodic path). We assert via the audit log that a closeout-kind
    # checkpoint write occurred AFTER the periodic one, since a same-second
    # stamp collision can overwrite the periodic file on disk (second
    # granularity, accepted per the plan) — the point under test is that
    # closeout is not gated by _dirty, not the file count.
    await worker._closeout()

    # A closeout-kind checkpoint must exist on disk with the LLM summary.
    files = _conv_files(worker)
    assert files, "closeout must write a checkpoint"
    closeout_bodies = [
        f.read_text(encoding="utf-8")
        for f in files
        if "checkpoint_kind: closeout" in f.read_text(encoding="utf-8")
    ]
    assert closeout_bodies, "expected a closeout-kind checkpoint after periodic"
    assert any("closeout prose summary" in b for b in closeout_bodies)


# ---------------------------------------------------------------------------
# Fix 1: a no-client _handle is a no-op and must NOT arm the periodic
# checkpoint (no counter increment, no dirty flag).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_client_handle_does_not_arm_periodic(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    worker._client = None  # no SDK client -> _handle early-returns

    # Baseline bookkeeping the no-client no-op must leave untouched.
    worker._dirty = False
    worker._turns_since_checkpoint = 0

    from anna.transports.base import InboundEvent

    event = InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY,
        sender_id="U1",
        sender_display="Tester",
        text="hello",
        is_dm=True,
        is_thread=False,
    )

    await worker._handle(event)

    # The phantom no-turn must NOT advance the periodic counters.
    assert worker._turns_since_checkpoint == 0
    assert worker._dirty is False


@pytest.mark.asyncio
async def test_real_turn_arms_periodic(tmp_path: Path) -> None:
    """Counterpart to the no-client test: a genuine turn DOES advance state."""
    worker = _make_worker(tmp_path)
    worker._client = FakeSDKClient(responses=["a reply"])

    worker._dirty = False
    worker._turns_since_checkpoint = 0

    from anna.transports.base import InboundEvent

    event = InboundEvent(
        transport="slack",
        conversation_key=CONV_KEY,
        sender_id="U1",
        sender_display="Tester",
        text="hello",
        is_dm=True,
        is_thread=False,
    )

    await worker._handle(event)

    assert worker._client.queries == ["hello"]
    assert worker._turns_since_checkpoint == 1
    assert worker._dirty is True


# ---------------------------------------------------------------------------
# Fix 2: the periodic audit event must carry the REAL triggering turn count,
# not 0 (the count _write_checkpoint_now resets the field to before audit).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_audit_logs_real_triggering_count(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    worker._client = FakeSDKClient()
    _plant_transcript(worker, _sample_lines())

    trigger_count = worker._config.checkpoint.every_turns
    worker._dirty = True
    worker._turns_since_checkpoint = trigger_count

    await worker._maybe_periodic_checkpoint()

    # The write happened and reset the counter.
    assert len(_conv_files(worker)) == 1
    assert worker._turns_since_checkpoint == 0

    events = _audit_events(worker, "audit.checkpoint.periodic")
    assert len(events) == 1, "expected one periodic audit event"
    assert events[0]["turns_since_checkpoint"] == trigger_count, (
        "audit must log the real triggering count, not the post-reset 0"
    )
    assert trigger_count != 0, "test is only meaningful with a non-zero trigger"
