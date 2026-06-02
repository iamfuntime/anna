"""Packaged installer templates.

Files in this package (currently just ``anna.service``, with
``com.iamfuntime.anna.plist`` arriving in Phase B) are read via
``importlib.resources`` by :mod:`anna.setup.wizard` and copied into the
operator's OS-appropriate location at wizard time.

Mirrors the loader pattern already established by
:mod:`anna.core_files`: the runtime never reads these files; the
templates are copied to operator-owned paths so the operator can edit
them.
"""
