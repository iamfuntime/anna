"""Validate ConversationWorker._closeout writes a checkpoint and runs eviction.

The worker normally drives a real ClaudeSDKClient. These tests inject a fake
client that returns canned text for the closeout summary query, then canned
JSON for each per-core-file eviction proposal. The runtime must:

* Write a checkpoint .md under vault/Conversations/<key>/.
* For each core file that is over its token cap, archive the evicted text
  to vault/Identity/<file>-archive-<date>.md and rewrite the core file.
* Be idempotent: a second stop() does NOT re-run the closeout.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from anna.config import AnnaConfig
from anna.core.identity import CORE_FILES, CoreFile
from anna.runtime.supervisor import Supervisor
from anna.runtime.worker import ConversationWorker


CONV_KEY = "slack:dm:UTEST"


# ---------------------------------------------------------------------------
# Fake SDK
# ---------------------------------------------------------------------------


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeAssistantMessage:
    content: list[Any]


@dataclass
class _FakeResultMessage:
    pass


class FakeSDKClient:
    """Returns ``responses`` in order, one per ``query()`` call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._pending: str | None = None
        self.queries: list[str] = []
        self.closed = False

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        if not self._responses:
            self._pending = "(out of canned responses)"
        else:
            self._pending = self._responses.pop(0)

    async def receive_response(self):
        text = self._pending or ""
        self._pending = None
        yield _FakeAssistantMessage(content=[_FakeTextBlock(text=text)])
        yield _FakeResultMessage()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        self.closed = True


# Patch the SDK message types the worker imports so isinstance() succeeds
# against our fakes. We monkeypatch claude_agent_sdk at import-time.
@pytest.fixture(autouse=True)
def _patch_sdk_types(monkeypatch):
    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "AssistantMessage", _FakeAssistantMessage, raising=False)
    monkeypatch.setattr(sdk, "ResultMessage", _FakeResultMessage, raising=False)
    monkeypatch.setattr(sdk, "TextBlock", _FakeTextBlock, raising=False)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker(tmp_path: Path) -> ConversationWorker:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    cfg.vault.path = str(tmp_path / "vault")
    cfg.logging.audit.fsync_on_write = False
    cfg.core_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(config=cfg)

    async def _noop_send(_msg):
        return None

    return ConversationWorker(
        conversation_key=CONV_KEY,
        transport="slack",
        config=cfg,
        supervisor=supervisor,
        send=_noop_send,
    )


def _over_cap_body(spec) -> str:
    # Generate a body deliberately above the token cap. count_tokens splits
    # on whitespace, so cap * 2 whitespace-separated words is comfortably over.
    return " ".join(["word"] * (spec.token_cap * 2))


def _eviction_payload(keep: str, evict: str, reason: str) -> str:
    return json.dumps({"keep_text": keep, "evict_text": evict, "reason": reason})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closeout_writes_checkpoint(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    # Plant a fake SDK with one canned summary.
    fake = FakeSDKClient(responses=["Topics: layout. Decisions: none. Open threads: none."])
    worker._client = fake

    await worker._closeout()

    safe = CONV_KEY.replace(":", "-")
    conv_dir = worker._config.vault.resolved_path / "Conversations" / safe
    assert conv_dir.is_dir()
    files = list(conv_dir.glob("*.md"))
    assert files, "expected a checkpoint file"
    body = files[0].read_text(encoding="utf-8")
    assert "Topics: layout" in body


@pytest.mark.asyncio
async def test_closeout_runs_eviction_for_over_cap_files(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    cfg = worker._config

    # MEMORY.md is over cap; the rest are empty (or short) so eviction is
    # skipped for them.
    memory_spec = CORE_FILES[CoreFile.MEMORY]
    (cfg.core_dir / "MEMORY.md").write_text(_over_cap_body(memory_spec), encoding="utf-8")

    # First canned response is the closeout summary. After that, the worker
    # asks the SDK for one eviction proposal per OVER-CAP file. Only MEMORY
    # is over cap.
    fake = FakeSDKClient(responses=[
        "Closing summary.",
        _eviction_payload(
            keep="kept content under cap",
            evict="evicted prose",
            reason="trimmed for cap",
        ),
    ])
    worker._client = fake

    await worker._closeout()

    # Checkpoint landed.
    safe = CONV_KEY.replace(":", "-")
    conv_dir = cfg.vault.resolved_path / "Conversations" / safe
    assert list(conv_dir.glob("*.md"))

    # MEMORY.md was rewritten with keep_text.
    assert (cfg.core_dir / "MEMORY.md").read_text(encoding="utf-8") == "kept content under cap"

    # Archive landed under vault/Identity/.
    identity_dir = cfg.vault.resolved_path / "Identity"
    archives = list(identity_dir.glob("MEMORY-archive-*.md"))
    assert archives, f"expected an archive; got {list(identity_dir.glob('*'))}"
    assert "evicted prose" in archives[0].read_text(encoding="utf-8")

    # The SDK was queried exactly twice: closeout summary + one eviction.
    assert len(fake.queries) == 2


@pytest.mark.asyncio
async def test_closeout_is_idempotent_via_stop(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    fake = FakeSDKClient(responses=["summary one", "summary two"])
    worker._client = fake
    # Skip the task lifecycle by faking ``start()`` so stop() doesn't need
    # to cancel anything; just call closeout manually then call stop twice.
    worker._task = None

    await worker.stop()
    queries_after_first = len(fake.queries)
    await worker.stop()
    assert len(fake.queries) == queries_after_first, "closeout ran twice"


@pytest.mark.asyncio
async def test_closeout_continues_on_eviction_failure(tmp_path: Path) -> None:
    """If one core file's eviction SDK call fails, others still proceed."""
    worker = _make_worker(tmp_path)
    cfg = worker._config

    # Make both MEMORY.md and CLAUDE.md over cap.
    mem_spec = CORE_FILES[CoreFile.MEMORY]
    cla_spec = CORE_FILES[CoreFile.CLAUDE]
    (cfg.core_dir / "MEMORY.md").write_text(_over_cap_body(mem_spec), encoding="utf-8")
    (cfg.core_dir / "CLAUDE.md").write_text(_over_cap_body(cla_spec), encoding="utf-8")

    # First response: closeout summary. Then per-file evictions in the
    # CORE_FILES iteration order (SOUL, CLAUDE, AGENTS, MEMORY, IDENTITY).
    # CLAUDE comes before MEMORY. Return garbage for CLAUDE (eviction
    # parser will skip it) and a valid payload for MEMORY.
    fake = FakeSDKClient(responses=[
        "summary",
        "not-json-at-all",
        _eviction_payload("kept", "evicted", "fine"),
    ])
    worker._client = fake

    await worker._closeout()

    # CLAUDE.md was NOT rewritten (proposal rejected).
    assert (cfg.core_dir / "CLAUDE.md").read_text(encoding="utf-8").startswith("word")
    # MEMORY.md WAS rewritten.
    assert (cfg.core_dir / "MEMORY.md").read_text(encoding="utf-8") == "kept"
