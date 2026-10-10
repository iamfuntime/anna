from pathlib import Path

from anna.runtime.memory import MemoryStore
from anna.tools.memory_server import MemoryTools


def test_remember_search_correct_forget_persists(tmp_path: Path) -> None:
    path = tmp_path / "state" / "memory.sqlite3"
    store = MemoryStore(path)
    fact = store.remember(owner_id="seth", visibility_scope="private", kind="preference", subject="coffee", content="prefers espresso", source_event_id="evt-1")
    assert store.search(owner_id="seth", visibility_scopes={"private"}, query="espresso") == [fact]
    corrected = store.correct(fact.id, content="prefers tea", source_event_id="evt-2")
    assert store.search(owner_id="seth", visibility_scopes={"private"}, query="espresso") == []
    assert store.search(owner_id="seth", visibility_scopes={"private"}, query="tea") == [corrected]
    store.forget(corrected.id)
    assert store.search(owner_id="seth", visibility_scopes={"private"}, query="tea") == []
    store.close()
    reopened = MemoryStore(path)
    assert reopened.get(fact.id).status == "superseded"
    assert reopened.get(corrected.id).status == "forgotten"


def test_private_scope_never_leaks_to_group(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.remember(owner_id="seth", visibility_scope="private", kind="account", subject="bank", content="private balance")
    store.remember(owner_id="seth", visibility_scope="group:project", kind="project", subject="release", content="ship Friday")
    assert [f.subject for f in store.search(owner_id="seth", visibility_scopes={"group:project"}, query="private OR ship")] == ["release"]


async def test_explicit_memory_tools_commit_correct_and_forget(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    tools = MemoryTools(store, owner_id="seth", scope="private", source_event_id=lambda: "telegram:99")
    response = await tools.remember(kind="preference", subject="tea", content="likes earl grey")
    assert "Remembered fact #1" in response["content"][0]["text"]
    assert store.get(1).source_event_id == "telegram:99"
    assert (await tools.search(query="earl"))["content"][0]["text"].startswith("#1 tea")
    corrected = await tools.correct(fact_id=1, content="likes oolong")
    assert "fact #2" in corrected["content"][0]["text"]
    assert "oolong" in (await tools.search(query="oolong"))["content"][0]["text"]
    forgotten = await tools.forget(fact_id=2)
    assert "Forgot memory #2" in forgotten["content"][0]["text"]


async def test_memory_tools_reject_cross_scope_mutation(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    private = MemoryTools(store, owner_id="seth", scope="private", source_event_id=lambda: None)
    group = MemoryTools(store, owner_id="seth", scope="group:release", source_event_id=lambda: None)
    await private.remember(kind="preference", subject="secret", content="private value")
    assert "outside" in (await group.forget(fact_id=1))["content"][0]["text"]
