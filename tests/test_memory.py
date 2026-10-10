from pathlib import Path

from anna.runtime.memory import MemoryStore


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
