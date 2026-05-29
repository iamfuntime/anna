"""Packaged templates for the five core identity files.

These are copied into ``$ANNA_HOME/core/`` by the setup wizard via
:func:`anna.core.identity.ensure_core_files`. The runtime never reads from
this package directly; it always reads the operator-owned copy on disk so
the operator can edit them.
"""
