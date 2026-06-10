"""Filesystem-safe path fragments derived from conversation keys.

A conv_key like ``slack:dm:USP2QLB41`` doubles as a directory name in the
vault (``Conversations/<safe-key>/``) and the transcript tree. ``:`` and
``/`` are unsafe or ambiguous in pathnames, so every writer and reader
applies the same transform via :func:`safe_conv_key`.

This module must stay import-light (stdlib only): it is imported from
``anna.log`` and the vault writers, which sit on hot paths and below most
of the package in the import graph.
"""

from __future__ import annotations


def safe_conv_key(conv_key: str) -> str:
    """Convert a conv_key into its filesystem-safe directory-name form.

    ``:`` becomes ``-`` and ``/`` becomes ``_``, e.g.
    ``slack:dm:USP2QLB41`` -> ``slack-dm-USP2QLB41``. Every checkpoint /
    transcript writer and reader must use this single transform so they
    agree on directory names; do not inline the replacements at call
    sites.
    """
    return conv_key.replace(":", "-").replace("/", "_")
