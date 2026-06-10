"""Tests for the TaskNote reader + pipeline board (MC-09).

Done conditions from the plan
(Inbox/2026-06-10-anna-web-mission-control-plan.md, subtask 9):

* With a fixture vault and both integration flags on, ``GET /tasks``
  renders the four kanban columns (Open / In progress / Review / Done)
  with TaskNotes bucketed by frontmatter status.
* With default config the route stays 404 (gating pinned in
  tests/test_web_integrations.py; re-asserted here for the new view).
* Enabled but with no ``tasknotes_path`` configured (or the directory
  missing) → the board renders 'No task data yet' rather than erroring.

Reader contract pinned at the unit level: status-variant bucketing,
title extraction, malformed-frontmatter degradation to the 'other'
bucket, the done-column cap, and the never-raise guarantee on missing
directories.

Fixture strategy mirrors :mod:`tests.test_web_integrations`: copy
``anna.yaml.example`` into a tmp home, build a fresh app via
``create_app`` with the integration flags + a tmp TaskNote directory,
exercise through :class:`fastapi.testclient.TestClient`.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from anna.config import AnnaConfig
from anna_web.app import create_app
from anna_web.readers.tasknote_reader import (
    BUCKET_DONE,
    BUCKET_IN_PROGRESS,
    BUCKET_OPEN,
    BUCKET_OTHER,
    BUCKET_REVIEW,
    DONE_COLUMN_CAP,
    TaskNoteReader,
    bucket_for_status,
)

REPO_EXAMPLE = Path(__file__).resolve().parent.parent / "anna.yaml.example"


# ---------------------------------------------------------------------------
# Fixtures + seed helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def anna_home(tmp_path: Path) -> Path:
    """Per-test fake ``$ANNA_HOME`` with a fresh copy of anna.yaml.example."""
    home = tmp_path / "anna_home"
    home.mkdir()
    shutil.copy(REPO_EXAMPLE, home / "anna.yaml")
    return home


@pytest.fixture
def notes_dir(tmp_path: Path) -> Path:
    """A tmp TaskNote directory standing in for the operator's vault."""
    directory = tmp_path / "TaskNotes" / "Tasks"
    directory.mkdir(parents=True)
    return directory


def _make_cfg(
    anna_home: Path,
    *,
    enabled: bool = False,
    tasknotes_path: Path | None = None,
) -> AnnaConfig:
    cfg = AnnaConfig()
    # Same override pattern as tests/test_web_integrations.py.
    object.__setattr__(cfg, "anna_home", anna_home)
    cfg.integrations.obsidian.enabled = enabled
    cfg.integrations.obsidian.tasknotes_enabled = enabled
    cfg.integrations.obsidian.tasknotes_path = tasknotes_path
    return cfg


def _client(
    anna_home: Path,
    *,
    enabled: bool = True,
    tasknotes_path: Path | None = None,
) -> TestClient:
    return TestClient(
        create_app(_make_cfg(anna_home, enabled=enabled, tasknotes_path=tasknotes_path))
    )


def _write_note(
    directory: Path,
    name: str,
    *,
    status: str | None = "todo",
    heading: str | None = None,
    assignee: str | None = "anna",
    priority: str | None = "normal",
    created: str | None = "2026-06-01",
    modified: str | None = None,
    completed: str | None = None,
) -> Path:
    """Write one TaskNotes-plugin-shaped markdown file (None omits a field)."""
    lines = ["---"]
    for key, value in (
        ("status", status),
        ("assignee", assignee),
        ("priority", priority),
        ("created", created),
        ("dateModified", modified),
        ("completedDate", completed),
    ):
        if value is not None:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    if heading is not None:
        lines.append(f"## {heading}")
        lines.append("")
    lines.append("Body text.")
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Reader: status bucketing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("open", BUCKET_OPEN),
        ("todo", BUCKET_OPEN),
        ("Todo", BUCKET_OPEN),
        ("in-progress", BUCKET_IN_PROGRESS),
        ("in progress", BUCKET_IN_PROGRESS),
        ("In Progress", BUCKET_IN_PROGRESS),
        ("in_progress", BUCKET_IN_PROGRESS),
        ("doing", BUCKET_IN_PROGRESS),
        ("review", BUCKET_REVIEW),
        ("done", BUCKET_DONE),
        ("DONE", BUCKET_DONE),
        ("cancelled", BUCKET_DONE),
        ("canceled", BUCKET_DONE),
        ("blocked", BUCKET_OTHER),
        ("", BUCKET_OTHER),
        ("  ", BUCKET_OTHER),
        (None, BUCKET_OTHER),
        (["todo", "done"], BUCKET_OTHER),  # YAML can hand back non-strings
        (True, BUCKET_OTHER),
    ],
)
def test_bucket_for_status(status: object, expected: str) -> None:
    assert bucket_for_status(status) == expected


def test_board_buckets_files_by_status(notes_dir: Path) -> None:
    _write_note(notes_dir, "a.md", status="todo", heading="Open one")
    _write_note(notes_dir, "b.md", status="open", heading="Open two")
    _write_note(notes_dir, "c.md", status="in-progress", heading="Doing one")
    _write_note(notes_dir, "d.md", status="review", heading="Review one")
    _write_note(notes_dir, "e.md", status="done", heading="Done one")
    _write_note(notes_dir, "f.md", status="cancelled", heading="Killed one")
    _write_note(notes_dir, "g.md", status="blocked", heading="Stray one")

    board = TaskNoteReader(notes_dir).board()
    assert board is not None
    assert sorted(n.title for n in board.open) == ["Open one", "Open two"]
    assert [n.title for n in board.in_progress] == ["Doing one"]
    assert [n.title for n in board.review] == ["Review one"]
    assert sorted(n.title for n in board.done) == ["Done one", "Killed one"]
    assert [n.title for n in board.other] == ["Stray one"]
    assert board.total == 7
    assert board.done_total == 2


def test_missing_status_buckets_as_other(notes_dir: Path) -> None:
    _write_note(notes_dir, "nostatus.md", status=None, heading="No status here")
    board = TaskNoteReader(notes_dir).board()
    assert board is not None
    assert [n.title for n in board.other] == ["No status here"]
    assert board.other[0].status == ""


# ---------------------------------------------------------------------------
# Reader: titles + metadata extraction
# ---------------------------------------------------------------------------


def test_title_prefers_first_heading_else_filename(notes_dir: Path) -> None:
    _write_note(notes_dir, "h2.md", heading="An H2 title")
    (notes_dir / "h1.md").write_text(
        "---\nstatus: todo\n---\n\n# An H1 title\n\nBody.\n", encoding="utf-8"
    )
    _write_note(notes_dir, "Bare Filename Task.md", heading=None)

    board = TaskNoteReader(notes_dir).board()
    assert board is not None
    titles = {n.filename: n.title for n in board.open}
    assert titles["h2.md"] == "An H2 title"
    assert titles["h1.md"] == "An H1 title"
    assert titles["Bare Filename Task.md"] == "Bare Filename Task"


def test_frontmatter_fields_extracted(notes_dir: Path) -> None:
    """Unquoted YAML dates (parsed as date/datetime objects) coerce to ISO."""
    _write_note(
        notes_dir,
        "full.md",
        status="done",
        heading="Fully dressed",
        assignee="anna",
        priority="High",
        created="2026-06-01",
        modified="2026-06-10T15:35:00.000-04:00",
        completed="2026-06-10",
    )
    board = TaskNoteReader(notes_dir).board()
    assert board is not None
    note = board.done[0]
    assert note.assignee == "anna"
    assert note.priority == "high"  # lowercased for display rules
    assert note.created == "2026-06-01"
    assert note.modified.startswith("2026-06-10T15:35:00")
    assert note.completed == "2026-06-10"
    assert note.bucket == BUCKET_DONE


# ---------------------------------------------------------------------------
# Reader: fail-soft contract
# ---------------------------------------------------------------------------


def test_malformed_frontmatter_lands_in_other_with_filename_title(
    notes_dir: Path,
) -> None:
    (notes_dir / "broken.md").write_text(
        "---\nstatus: [unclosed\n---\n\n## A heading that must NOT win\n",
        encoding="utf-8",
    )
    board = TaskNoteReader(notes_dir).board()
    assert board is not None
    assert board.total == 1
    assert [n.title for n in board.other] == ["broken"]


def test_unterminated_fence_lands_in_other(notes_dir: Path) -> None:
    (notes_dir / "unterminated.md").write_text(
        "---\nstatus: todo\nno closing fence", encoding="utf-8"
    )
    board = TaskNoteReader(notes_dir).board()
    assert board is not None
    assert [n.title for n in board.other] == ["unterminated"]


def test_missing_directory_returns_none(tmp_path: Path) -> None:
    assert TaskNoteReader(tmp_path / "nope").board() is None


def test_unset_path_returns_none() -> None:
    assert TaskNoteReader(None).board() is None


def test_path_pointing_at_a_file_returns_none(tmp_path: Path) -> None:
    target = tmp_path / "not_a_dir.md"
    target.write_text("x", encoding="utf-8")
    assert TaskNoteReader(target).board() is None


def test_empty_directory_yields_empty_board(notes_dir: Path) -> None:
    board = TaskNoteReader(notes_dir).board()
    assert board is not None
    assert board.total == 0


def test_non_markdown_and_subdirectories_ignored(notes_dir: Path) -> None:
    """*.md directly under the path only — Archive/ stays invisible."""
    _write_note(notes_dir, "real.md", heading="The only one")
    (notes_dir / "notes.txt").write_text("not markdown", encoding="utf-8")
    archive = notes_dir / "Archive"
    archive.mkdir()
    _write_note(archive, "archived.md", heading="Must not appear")

    board = TaskNoteReader(notes_dir).board()
    assert board is not None
    assert board.total == 1
    assert board.open[0].title == "The only one"


# ---------------------------------------------------------------------------
# Reader: done-column cap
# ---------------------------------------------------------------------------


def test_done_column_caps_at_most_recently_modified(notes_dir: Path) -> None:
    for day in range(1, DONE_COLUMN_CAP + 4):  # 18 done notes
        _write_note(
            notes_dir,
            f"done-{day:02d}.md",
            status="done",
            heading=f"Done {day:02d}",
            modified=f"2026-05-{day:02d}T10:00:00",
        )
    board = TaskNoteReader(notes_dir).board()
    assert board is not None
    assert board.done_total == DONE_COLUMN_CAP + 3
    assert len(board.done) == DONE_COLUMN_CAP
    # Newest dateModified first; the three oldest fell off the cap.
    assert board.done[0].title == f"Done {DONE_COLUMN_CAP + 3:02d}"
    kept = {n.title for n in board.done}
    assert {"Done 01", "Done 02", "Done 03"}.isdisjoint(kept)


# ---------------------------------------------------------------------------
# Reader: config gate (belt-and-braces on top of route mounting)
# ---------------------------------------------------------------------------


def test_from_config_disabled_returns_none(anna_home: Path) -> None:
    cfg = _make_cfg(anna_home, enabled=False)
    assert TaskNoteReader.from_config(cfg) is None


def test_from_config_enabled_builds_reader(anna_home: Path, notes_dir: Path) -> None:
    cfg = _make_cfg(anna_home, enabled=True, tasknotes_path=notes_dir)
    reader = TaskNoteReader.from_config(cfg)
    assert reader is not None
    assert reader.board() is not None


# ---------------------------------------------------------------------------
# View: the enabled board
# ---------------------------------------------------------------------------


def _seed_pipeline(notes_dir: Path) -> None:
    _write_note(
        notes_dir,
        "open-task.md",
        status="todo",
        heading="Ship the gizmo",
        priority="high",
        created=date.today().isoformat(),
    )
    _write_note(notes_dir, "doing-task.md", status="in-progress", heading="Wiring it up")
    _write_note(notes_dir, "review-task.md", status="review", heading="Awaiting eyes")
    _write_note(
        notes_dir,
        "done-task.md",
        status="done",
        heading="Already shipped",
        completed="2026-06-09",
    )


def test_board_renders_four_columns_with_bucketed_cards(
    anna_home: Path, notes_dir: Path
) -> None:
    _seed_pipeline(notes_dir)
    client = _client(anna_home, tasknotes_path=notes_dir)

    response = client.get("/tasks")
    assert response.status_code == 200
    body = response.text

    i_open = body.index('id="task-column-open"')
    i_prog = body.index('id="task-column-in-progress"')
    i_review = body.index('id="task-column-review"')
    i_done = body.index('id="task-column-done"')
    assert i_open < i_prog < i_review < i_done

    # Each card lands inside its own column's slice of the page.
    assert i_open < body.index("Ship the gizmo") < i_prog
    assert i_prog < body.index("Wiring it up") < i_review
    assert i_review < body.index("Awaiting eyes") < i_done
    assert body.index("Already shipped") > i_done

    # Card anatomy: mono filename, assignee badge, non-normal priority
    # badge, age chip off the created date.
    assert "open-task.md" in body
    assert 'class="badge badge-idle task-assignee">anna<' in body
    assert 'task-priority">high<' in body
    assert ">today</span>" in body  # created today → age chip "today"
    assert "No task data yet" not in body


def test_normal_priority_renders_no_badge(anna_home: Path, notes_dir: Path) -> None:
    _write_note(notes_dir, "plain.md", status="todo", heading="Plain", priority="normal")
    client = _client(anna_home, tasknotes_path=notes_dir)
    body = client.get("/tasks").text
    assert "task-priority" not in body


def test_unrecognized_status_folds_into_other_section(
    anna_home: Path, notes_dir: Path
) -> None:
    _write_note(notes_dir, "stray.md", status="blocked", heading="A stray")
    client = _client(anna_home, tasknotes_path=notes_dir)
    body = client.get("/tasks").text
    assert 'id="tasks-other"' in body
    assert "1 note with unrecognized status" in body
    assert "A stray" in body
    assert "task-card-other" in body


def test_board_page_polls_partial_every_15s(anna_home: Path, notes_dir: Path) -> None:
    _seed_pipeline(notes_dir)
    client = _client(anna_home, tasknotes_path=notes_dir)
    body = client.get("/tasks").text
    assert 'hx-get="/tasks/board"' in body
    assert 'hx-trigger="every 15s"' in body
    assert 'hx-swap="innerHTML"' in body


def test_partial_route_serves_columns_without_page_chrome(
    anna_home: Path, notes_dir: Path
) -> None:
    _seed_pipeline(notes_dir)
    client = _client(anna_home, tasknotes_path=notes_dir)
    response = client.get("/tasks/board")
    assert response.status_code == 200
    assert 'id="task-column-open"' in response.text
    assert "<nav>" not in response.text  # partial, not the full page


def test_board_page_highlights_tasks_nav(anna_home: Path, notes_dir: Path) -> None:
    client = _client(anna_home, tasknotes_path=notes_dir)
    body = client.get("/tasks").text
    assert '<a href="/tasks" aria-current="page">Tasks</a>' in body


# ---------------------------------------------------------------------------
# View: graceful degradation + gating
# ---------------------------------------------------------------------------


def test_enabled_without_tasknotes_path_renders_empty_state(anna_home: Path) -> None:
    client = _client(anna_home, tasknotes_path=None)
    response = client.get("/tasks")
    assert response.status_code == 200
    assert "No task data yet" in response.text


def test_enabled_with_missing_directory_renders_empty_state(
    anna_home: Path, tmp_path: Path
) -> None:
    client = _client(anna_home, tasknotes_path=tmp_path / "does-not-exist")
    response = client.get("/tasks")
    assert response.status_code == 200
    assert "No task data yet" in response.text


def test_enabled_with_empty_directory_renders_empty_state(
    anna_home: Path, notes_dir: Path
) -> None:
    client = _client(anna_home, tasknotes_path=notes_dir)
    response = client.get("/tasks")
    assert response.status_code == 200
    assert "No task data yet" in response.text


def test_disabled_keeps_tasks_404(anna_home: Path, notes_dir: Path) -> None:
    """Gating survives the real view: disabled → page AND partial 404."""
    _seed_pipeline(notes_dir)
    client = _client(anna_home, enabled=False, tasknotes_path=notes_dir)
    assert client.get("/tasks").status_code == 404
    assert client.get("/tasks/board").status_code == 404


def test_tasks_routes_stay_get_only(anna_home: Path, notes_dir: Path) -> None:
    """READ-ONLY v1 — no mutating routes anywhere under /tasks."""
    app = create_app(_make_cfg(anna_home, enabled=True, tasknotes_path=notes_dir))
    tasks_routes = [
        route for route in app.routes if getattr(route, "path", "").startswith("/tasks")
    ]
    assert len(tasks_routes) >= 2  # page + poll partial
    for route in tasks_routes:
        methods = getattr(route, "methods", None) or set()
        assert set(methods) <= {"GET", "HEAD"}
