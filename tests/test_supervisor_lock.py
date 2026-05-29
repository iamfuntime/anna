"""Validate the supervisor serializes core-file writes through asyncio.Lock."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anna.config import AnnaConfig
from anna.core.identity import CoreFile
from anna.runtime.supervisor import CoreFilePoisonedError, Supervisor


def _make_config(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path)
    return cfg


@pytest.mark.asyncio
async def test_concurrent_writes_serialize(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    sup = Supervisor(config=cfg)

    write_order: list[str] = []
    enter_order: list[str] = []

    async def writer(label: str, sleep: float) -> None:
        # Inject a small delay before acquiring to encourage interleaving.
        await asyncio.sleep(sleep)
        enter_order.append(label)
        await sup.write_core_file(
            CoreFile.MEMORY,
            new_content=f"# {label}\n",
            reason=f"test-{label}",
            conv_key=f"test:{label}",
        )
        write_order.append(label)

    # Submit several writers in parallel; the lock must serialize them so the
    # final file content reflects the last writer to run, with no torn writes.
    await asyncio.gather(
        writer("a", 0.00),
        writer("b", 0.00),
        writer("c", 0.00),
    )

    # All three completed.
    assert sorted(write_order) == ["a", "b", "c"]

    # File content is one whole writer's payload, never a mix.
    text = (tmp_path / "core" / "MEMORY.md").read_text(encoding="utf-8")
    assert text in {"# a\n", "# b\n", "# c\n"}


@pytest.mark.asyncio
async def test_poisoned_file_refuses_writes(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    sup = Supervisor(config=cfg)

    sup.poison("MEMORY.md", reason="test setup")
    with pytest.raises(CoreFilePoisonedError):
        await sup.write_core_file(
            CoreFile.MEMORY,
            new_content="ignored",
            reason="should fail",
            conv_key="test",
        )

    # After unpoison the write goes through.
    cleared = sup.unpoison("MEMORY.md", actor="operator")
    assert cleared is True
    await sup.write_core_file(
        CoreFile.MEMORY,
        new_content="ok",
        reason="now allowed",
        conv_key="test",
    )
    assert (tmp_path / "core" / "MEMORY.md").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_arbitrary_lock_acquire_is_idempotent(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    sup = Supervisor(config=cfg)

    a = await sup.acquire("agents/foo")
    b = await sup.acquire("agents/foo")
    assert a is b
