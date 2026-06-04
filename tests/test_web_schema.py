"""Tests for ``anna_web.schema.describe`` (subtask 5).

Covers every mapping rule in the Phase 2.5 plan's "Schema-driven form
generation" section, plus the load-bearing integration assertion that
:class:`anna.config.AnnaConfig` walks cleanly end-to-end with no
``NotImplementedError`` and no missing-kind branches. If the
integration test breaks, subtask 7's ``/config`` route fails to
render — so this file is the canary for the whole dashboard pipeline.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from anna.config import AnnaConfig, WebDashboardConfig
from anna_web.schema import FieldKind, FormField, describe


# ---------------------------------------------------------------------------
# 1. bool → CHECKBOX
# ---------------------------------------------------------------------------


def test_bool_maps_to_checkbox() -> None:
    class M(BaseModel):
        x: bool = True

    fields = describe(M, M())
    assert len(fields) == 1
    f = fields[0]
    assert f.name == "x"
    assert f.kind is FieldKind.CHECKBOX
    assert f.value is True


def test_bool_no_values_leaves_value_none() -> None:
    class M(BaseModel):
        x: bool = True

    fields = describe(M)
    assert fields[0].value is None


# ---------------------------------------------------------------------------
# 2. int / float → NUMBER
# ---------------------------------------------------------------------------


def test_int_and_float_map_to_number() -> None:
    class M(BaseModel):
        x: int = 8765
        y: float = 1.5

    fields = describe(M, M())
    by_name = {f.name: f for f in fields}
    assert by_name["x"].kind is FieldKind.NUMBER
    assert by_name["x"].value == 8765
    assert by_name["y"].kind is FieldKind.NUMBER
    assert by_name["y"].value == 1.5


# ---------------------------------------------------------------------------
# 3. str → TEXT
# ---------------------------------------------------------------------------


def test_str_maps_to_text() -> None:
    class M(BaseModel):
        x: str = "hi"

    fields = describe(M, M())
    assert fields[0].kind is FieldKind.TEXT
    assert fields[0].value == "hi"


# ---------------------------------------------------------------------------
# 4. str with widget=textarea → TEXTAREA
# ---------------------------------------------------------------------------


def test_str_with_textarea_widget_maps_to_textarea() -> None:
    class M(BaseModel):
        prompt: str = Field(default="", json_schema_extra={"widget": "textarea"})

    fields = describe(M, M())
    assert fields[0].kind is FieldKind.TEXTAREA


def test_str_without_textarea_widget_stays_text() -> None:
    class M(BaseModel):
        prompt: str = Field(default="", description="Some prose")

    fields = describe(M, M())
    assert fields[0].kind is FieldKind.TEXT


# ---------------------------------------------------------------------------
# 5. Literal[...] → SELECT
# ---------------------------------------------------------------------------


def test_literal_maps_to_select_with_options() -> None:
    class M(BaseModel):
        x: Literal["a", "b", "c"] = "a"

    fields = describe(M, M())
    f = fields[0]
    assert f.kind is FieldKind.SELECT
    assert f.options == ["a", "b", "c"]
    assert f.value == "a"


# ---------------------------------------------------------------------------
# 6. list[str] → REPEATED_TEXT
# ---------------------------------------------------------------------------


def test_list_of_str_maps_to_repeated_text() -> None:
    class M(BaseModel):
        xs: list[str] = Field(default_factory=list)

    fields = describe(M, M(xs=["one", "two"]))
    f = fields[0]
    assert f.kind is FieldKind.REPEATED_TEXT
    assert f.value == ["one", "two"]


def test_list_of_str_empty_when_no_values() -> None:
    class M(BaseModel):
        xs: list[str] = Field(default_factory=list)

    fields = describe(M)
    assert fields[0].kind is FieldKind.REPEATED_TEXT
    assert fields[0].value == []


# ---------------------------------------------------------------------------
# 7. list[BaseModel] → REPEATED_FIELDSET
# ---------------------------------------------------------------------------


def test_list_of_basemodel_maps_to_repeated_fieldset() -> None:
    class Item(BaseModel):
        slug: str
        count: int = 0

    class M(BaseModel):
        items: list[Item] = Field(default_factory=list)

    fields = describe(M, M(items=[Item(slug="a", count=1), Item(slug="b", count=2)]))
    f = fields[0]
    assert f.kind is FieldKind.REPEATED_FIELDSET

    # item_template must be the shape of one empty item
    assert f.item_template is not None
    template_names = [t.name for t in f.item_template]
    assert template_names == ["slug", "count"]
    # Template values are None (empty form)
    assert all(t.value is None for t in f.item_template)

    # children: one fieldset per current item, with value pre-filled
    assert len(f.children) == 2
    row0, row1 = f.children
    assert row0.kind is FieldKind.FIELDSET
    assert row1.kind is FieldKind.FIELDSET
    by_name0 = {c.name: c for c in row0.children}
    assert by_name0["slug"].value == "a"
    assert by_name0["count"].value == 1
    by_name1 = {c.name: c for c in row1.children}
    assert by_name1["slug"].value == "b"
    assert by_name1["count"].value == 2


# ---------------------------------------------------------------------------
# 8. Nested BaseModel → FIELDSET (using the real WebDashboardConfig)
# ---------------------------------------------------------------------------


def test_nested_basemodel_maps_to_fieldset() -> None:
    fields = describe(AnnaConfig, AnnaConfig())
    by_name = {f.name: f for f in fields}
    web = by_name["web"]
    assert web.kind is FieldKind.FIELDSET
    child_names = {c.name for c in web.children}
    # WebDashboardConfig fields:
    assert {"enabled", "host", "port", "target_unit"}.issubset(child_names)
    by_child = {c.name: c for c in web.children}
    assert by_child["enabled"].kind is FieldKind.CHECKBOX
    assert by_child["enabled"].value is True
    assert by_child["host"].kind is FieldKind.TEXT
    assert by_child["host"].value == "127.0.0.1"
    assert by_child["port"].kind is FieldKind.NUMBER
    assert by_child["port"].value == 8765


# ---------------------------------------------------------------------------
# 9. Optional[X] unwraps correctly
# ---------------------------------------------------------------------------


def test_optional_x_or_none_marks_not_required_pep604() -> None:
    class M(BaseModel):
        x: str | None = None

    fields = describe(M, M())
    f = fields[0]
    assert f.kind is FieldKind.TEXT
    assert f.required is False
    assert f.value is None


def test_optional_x_typing_marks_not_required() -> None:
    class M(BaseModel):
        x: Optional[int] = None  # noqa: UP045 — explicit typing.Optional case

    fields = describe(M, M())
    f = fields[0]
    assert f.kind is FieldKind.NUMBER
    assert f.required is False


def test_non_optional_field_marked_required() -> None:
    class M(BaseModel):
        x: str

    fields = describe(M)
    assert fields[0].required is True


# ---------------------------------------------------------------------------
# 10. Path dotted-prefix and indexed paths
# ---------------------------------------------------------------------------


def test_top_level_path_is_field_name() -> None:
    fields = describe(AnnaConfig)
    by_name = {f.name: f for f in fields}
    assert by_name["web"].path == "web"


def test_nested_field_path_uses_dotted_prefix() -> None:
    fields = describe(AnnaConfig)
    web = next(f for f in fields if f.name == "web")
    by_child = {c.name: c for c in web.children}
    assert by_child["enabled"].path == "web.enabled"
    assert by_child["host"].path == "web.host"


def test_repeated_fieldset_items_use_indexed_paths() -> None:
    # Stick to the real identities model so the production schema gets
    # exercised end-to-end.
    from anna.config import IdentityAliasEntry

    cfg = AnnaConfig(
        identities=[
            IdentityAliasEntry(canonical="seth", slack_user_id="U0SETH"),
            IdentityAliasEntry(canonical="ops", telegram_chat_id="12345"),
        ]
    )
    fields = describe(AnnaConfig, cfg)
    identities = next(f for f in fields if f.name == "identities")
    assert identities.kind is FieldKind.REPEATED_FIELDSET
    assert len(identities.children) == 2
    row0 = identities.children[0]
    assert row0.path == "identities[0]"
    by_child0 = {c.name: c for c in row0.children}
    assert by_child0["canonical"].path == "identities[0].canonical"
    row1 = identities.children[1]
    assert row1.path == "identities[1]"


# ---------------------------------------------------------------------------
# 11. AnnaConfig integration — the load-bearing test
# ---------------------------------------------------------------------------


def test_anna_config_describes_cleanly_with_full_schema() -> None:
    """The whole schema walks without raising and yields one FormField per
    top-level field.

    If this breaks, subtask 7's ``/config`` route cannot render. Treat
    failures here as a release-blocker for the dashboard.
    """
    fields = describe(AnnaConfig, AnnaConfig())
    names = [f.name for f in fields]
    # Every top-level pydantic field (minus excluded ``anna_home``)
    # should be present exactly once and in declaration order.
    expected = [
        n for n in AnnaConfig.model_fields if n != "anna_home"
    ]
    assert names == expected

    # No kind should be missing — i.e. every FormField has a real
    # FieldKind value (already enforced by the dataclass, but assert
    # we got the right type back).
    for f in fields:
        assert isinstance(f, FormField)
        assert isinstance(f.kind, FieldKind)

    # ``anna_home`` is NOT in the rendered set.
    assert "anna_home" not in names


def test_anna_config_describe_no_values_works() -> None:
    """Same walk with values=None — must not crash and must produce the
    same field shape (just with value=None at the leaves)."""
    fields = describe(AnnaConfig, None)
    names = [f.name for f in fields]
    expected = [
        n for n in AnnaConfig.model_fields if n != "anna_home"
    ]
    assert names == expected
    web = next(f for f in fields if f.name == "web")
    assert web.kind is FieldKind.FIELDSET
    # Children should all carry value=None when no values passed
    for c in web.children:
        assert c.value is None


def test_anna_config_describe_from_dict_works() -> None:
    """describe() must accept a dict (model_dump shape) as values."""
    cfg = AnnaConfig()
    fields = describe(AnnaConfig, cfg.model_dump())
    web = next(f for f in fields if f.name == "web")
    by_child = {c.name: c for c in web.children}
    assert by_child["port"].value == 8765
    assert by_child["enabled"].value is True


# ---------------------------------------------------------------------------
# Misc: WebDashboardConfig direct describe is sane (sanity for subtask 7's
# section-level rendering path).
# ---------------------------------------------------------------------------


def test_web_dashboard_config_described_directly() -> None:
    fields = describe(WebDashboardConfig, WebDashboardConfig())
    by_name = {f.name: f for f in fields}
    assert by_name["enabled"].kind is FieldKind.CHECKBOX
    assert by_name["host"].kind is FieldKind.TEXT
    assert by_name["port"].kind is FieldKind.NUMBER
    assert by_name["target_unit"].kind is FieldKind.TEXT
    # Top-level call (no parent) → path is just the field name
    assert by_name["enabled"].path == "enabled"


# ---------------------------------------------------------------------------
# json_schema_extra propagation onto FormField.extra (subtask 10's metadata
# channel for the bind-host warning).
# ---------------------------------------------------------------------------


def test_field_extra_propagates_json_schema_extra() -> None:
    """A field's ``json_schema_extra`` lands on ``FormField.extra``."""

    class M(BaseModel):
        x: str = Field(default="", json_schema_extra={"warn_if_non_loopback": True})
        y: str = "plain"

    by_name = {f.name: f for f in describe(M, M())}
    assert by_name["x"].extra.get("warn_if_non_loopback") is True
    # A field without json_schema_extra gets an empty dict, not None.
    assert by_name["y"].extra == {}


def test_web_host_field_carries_warn_flag() -> None:
    """The real ``web.host`` field flags itself for the non-loopback warning."""
    fields = describe(AnnaConfig, AnnaConfig())
    web = next(f for f in fields if f.name == "web")
    host = next(c for c in web.children if c.name == "host")
    assert host.extra.get("warn_if_non_loopback") is True
