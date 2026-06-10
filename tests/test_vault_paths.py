"""Unit tests for ``anna.vault.paths``.

Covers the canonical conv_key -> filesystem-safe transform shared by the
checkpoint writer/reader, the transcript directory naming in ``anna.log``,
and ``anna-admin merge-checkpoints``.
"""

from __future__ import annotations

from anna.vault.paths import safe_conv_key


def test_safe_conv_key_maps_separators() -> None:
    # ``:`` becomes ``-`` ...
    assert safe_conv_key("slack:dm:USP2QLB41") == "slack-dm-USP2QLB41"
    assert safe_conv_key("user:seth") == "user-seth"
    # ... and pre-existing ``-`` in the key is preserved untouched.
    assert safe_conv_key("cli:oneshot:abc-123") == "cli-oneshot-abc-123"
    # ``/`` becomes ``_`` so a key can never escape its directory.
    assert safe_conv_key("slack:ch:team/general") == "slack-ch-team_general"
