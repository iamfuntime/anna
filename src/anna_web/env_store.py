"""Round-trip read/write of ``$ANNA_HOME/.env`` for the web dashboard.

Subtask 4 of the Phase 2.5 buildout. The :class:`EnvStore` is the
sole writer of the operator's ``.env`` file from the web surface. It
wraps :mod:`python-dotenv` so existing quoting, ordering, and unset
keys behave the way dotenv has always behaved at the CLI, and adds
two guarantees on top:

* **Documented-key allow-list.** By default the store refuses to
  ``set`` or ``delete`` a key that is not in :data:`DOCUMENTED_VARS`
  (parsed from ``.env.example`` at import time). The free-form
  "extra rows" section in the dashboard explicitly opts out via
  ``allow_unknown=True``. Default behavior fails closed so a typo
  cannot silently land an unsupported variable.

* **Permissions discipline.** After every successful mutation the
  store re-applies ``0o600`` on the dotenv file. python-dotenv's
  ``set_key`` has been observed to widen permissions on some
  filesystems; we re-tighten unconditionally so secrets never sit
  world-readable even briefly. If the file is missing at the chmod
  call (a race we don't expect but want to survive), we swallow
  :class:`FileNotFoundError` quietly — the next mutation will create
  it fresh at the correct mode.

Audit-event emission for ``audit.web.dashboard.secret_*`` is
deliberately out of scope here — that wiring lands with subtask 12.
The ``actor`` kwarg is on every mutating method so subtask 8's
route layer never has to refactor call sites when audit arrives.

See ``Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md``, "Architecture →
EnvStore — secrets handling" for the full design.
"""

from __future__ import annotations

import importlib.resources as importlib_resources
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

from anna.log import get_logger

# Operational logger, same structlog surface the rest of the web
# package uses (see anna_web.restart / anna_web.audit). Used only for
# the empty-documented-vars regression guard below; normal operation
# stays silent.
_log = get_logger("anna.web.env_store")

# Tokens in the env-var KEY that flip the rendered row to a masked
# input. Anything that smells like a credential gets a reveal-toggle;
# everything else is plain text. Conservative on purpose — better to
# mask a non-secret than to leak one.
_SECRET_TOKENS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASS", "WEBHOOK")

# Repo-root fallback for the editable dev tree. The package lives at
# ``src/anna_web/env_store.py``; the example sits at the repo root,
# three parents up from this file.
_DEV_ENV_EXAMPLE = Path(__file__).resolve().parent.parent.parent / ".env.example"


def _resolve_env_example() -> Path:
    """Locate ``.env.example`` in both the installed wheel and dev tree.

    The deployed dashboard runs from a uv-tool wheel where the old
    ``parent.parent.parent`` heuristic overshoots to a nonexistent
    path, leaving :data:`DOCUMENTED_VARS` empty and the Secrets page
    with zero Documented rows. We now prefer the copy packaged
    alongside the ``anna_web`` package (force-included by pyproject's
    wheel build), and fall back to the repo-root file only when the
    packaged copy is absent — i.e. an editable install off the source
    tree, where no wheel data was staged.
    """
    try:
        packaged = importlib_resources.files("anna_web") / ".env.example"
        candidate = Path(str(packaged))
        if candidate.is_file():
            return candidate
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        # ModuleNotFoundError: anna_web not importable (shouldn't happen
        # from inside it). FileNotFoundError/TypeError: defensive against
        # non-filesystem resource backends. Fall through to the dev tree.
        pass
    return _DEV_ENV_EXAMPLE


# Resolve the canonical .env.example once at import time.
_ENV_EXAMPLE = _resolve_env_example()

# Matches a ``KEY=`` or ``KEY=value`` line. We deliberately accept
# the dotenv-comment-prefix form (``# KEY=...``) as well, because
# .env.example documents most optional credentials commented-out so
# the wizard doesn't accidentally pick up empty placeholders. Either
# style means "this variable is documented".
_KEY_LINE = re.compile(r"^\s*(?:#\s*)?([A-Z][A-Z0-9_]*)\s*=")


@dataclass(frozen=True)
class DocumentedVar:
    """One documented environment variable parsed from ``.env.example``.

    The dashboard renders one labeled row per entry. ``kind`` drives
    whether the input is masked; ``description`` shows up as help
    text under the field.
    """

    name: str
    label: str
    kind: str  # "text" or "secret"
    description: str


def _label_from_name(name: str) -> str:
    """Title-case an env-var name into a human label.

    ``SLACK_BOT_TOKEN`` → ``Slack Bot Token``. Underscores become
    spaces; each word is title-cased. We do not try to special-case
    acronyms (URL, API) — the dashboard's job is to show the
    operator what variable is what, not to win a typography contest.
    """
    return " ".join(part.title() for part in name.split("_") if part)


def _kind_from_name(name: str) -> str:
    """Decide ``text`` vs ``secret`` from the key name.

    Any of ``TOKEN``, ``KEY``, ``SECRET``, ``PASSWORD``, ``PASS``,
    ``WEBHOOK`` anywhere in the key flips the kind to ``secret`` so
    the row gets masking + a reveal toggle.
    """
    return "secret" if any(tok in name for tok in _SECRET_TOKENS) else "text"


def _parse_env_example(path: Path) -> list[DocumentedVar]:
    """Walk ``.env.example`` and build the canonical documented list.

    Parsing rules (from the buildout plan):

    * A run of consecutive ``# ...`` lines immediately above a key
      line becomes that key's description, joined with single
      spaces. Banner separators (lines that are all ``=`` or all
      ``-``) are dropped so we don't render decorative chrome as
      help text.
    * A line matching :data:`_KEY_LINE` declares a documented
      variable. We accept commented-out forms (``# KEY=``) because
      most optional credentials in this repo's example are
      commented-out placeholders.
    * Duplicate keys (someone shipped two ``SLACK_BOT_TOKEN``
      stanzas) collapse to the first one encountered; we don't
      stack rows for the same env var.

    If the file does not exist (shouldn't happen in this repo, but
    defensive for downstream forks that strip the example) the
    returned list is empty and the dashboard falls back to the
    free-form "extra rows" UI for everything.
    """
    if not path.exists():
        return []

    documented: list[DocumentedVar] = []
    seen: set[str] = set()
    pending_comments: list[str] = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line:
            # Blank line breaks the comment-accumulator. Header
            # banners followed by a blank then a key shouldn't bleed
            # into the next variable's description.
            pending_comments = []
            continue

        if line.startswith("#"):
            # Commented-out key lines (``# KEY=``) are documented
            # variables, NOT description chrome — handle them in the
            # key-line branch below.
            if _KEY_LINE.match(line):
                pass  # fall through to the key-line handling
            else:
                # Strip the leading ``#`` and surrounding space.
                stripped = line.lstrip("#").strip()
                # Drop decorative separators (``===``, ``---``) so
                # they don't appear as help text.
                if stripped and set(stripped) not in ({"="}, {"-"}):
                    pending_comments.append(stripped)
                continue

        match = _KEY_LINE.match(line)
        if not match:
            # Anything else (rare: a stray non-comment, non-key
            # line) resets the comment buffer to avoid mis-
            # attributing.
            pending_comments = []
            continue

        name = match.group(1)
        if name in seen:
            pending_comments = []
            continue
        seen.add(name)

        description = " ".join(pending_comments).strip()
        documented.append(
            DocumentedVar(
                name=name,
                label=_label_from_name(name),
                kind=_kind_from_name(name),
                description=description,
            )
        )
        pending_comments = []

    return documented


# Module-level constant: parsed once at import time so callers can
# treat this as immutable canon. The set version is used internally
# for fast allow-list membership checks in :meth:`EnvStore.set` /
# :meth:`EnvStore.delete`.
DOCUMENTED_VARS: list[DocumentedVar] = _parse_env_example(_ENV_EXAMPLE)
_DOCUMENTED_NAMES: frozenset[str] = frozenset(v.name for v in DOCUMENTED_VARS)

# Loud regression guard. An empty documented list means the Secrets
# page renders zero Documented rows — almost always a packaging
# regression (``.env.example`` not shipped in the wheel) rather than an
# intentional state. Surface it at WARNING so it shows up in journald
# instead of silently emptying the page. Normal operation, where the
# file resolves and parses, stays silent.
if not DOCUMENTED_VARS:
    _log.warning(
        "anna.web.env.documented_vars_empty",
        env_example_path=str(_ENV_EXAMPLE),
        hint=(
            "No documented env vars parsed from .env.example; the Secrets "
            "page will show zero Documented rows. Check that .env.example is "
            "packaged into the wheel (pyproject force-include)."
        ),
    )


class EnvStore:
    """Editor for ``$ANNA_HOME/.env`` backed by :mod:`python-dotenv`.

    Instances are intended to be per-process singletons hung off
    ``app.state.env_store`` by the FastAPI app factory. Methods are
    synchronous: dotenv is file-IO-bound and the operator surface is
    one human clicking buttons, so the extra machinery of async + a
    thread pool would only add latency.
    """

    def __init__(self, *, anna_home: Path) -> None:
        self._path = anna_home / ".env"

    @property
    def path(self) -> Path:
        """Absolute path the store reads and writes."""
        return self._path

    def load(self) -> dict[str, str]:
        """Return every currently-set key as a plain dict.

        Backs the masked-list GET ``/env`` endpoint. A missing file
        returns ``{}`` — the operator may be setting up a fresh
        install and we shouldn't blow up on a not-yet-created
        ``.env``. dotenv returns ``Optional[str]`` for unset keys; we
        coerce ``None`` to the empty string so the route layer can
        treat the dict as ``str → str`` without an isinstance dance.
        """
        if not self._path.exists():
            return {}
        raw = dotenv_values(str(self._path))
        return {key: (value if value is not None else "") for key, value in raw.items()}

    def get(self, key: str) -> str | None:
        """Return one value or ``None`` if the key is unset.

        Backs the reveal endpoint (GET ``/env/{key}/reveal``).
        Returning ``None`` for both "file missing" and "key not in
        file" matches dotenv's own surface and keeps the route layer
        simple — a single 404 mapping covers both.
        """
        if not self._path.exists():
            return None
        return dotenv_values(str(self._path)).get(key)

    def set(
        self,
        key: str,
        value: str,
        *,
        actor: str = "operator",
        allow_unknown: bool = False,
    ) -> None:
        """Write or update one key, then re-tighten file permissions.

        Uses ``quote_mode="always"`` so values with spaces, ``#``,
        equals signs, or shell-meaningful characters survive the
        round-trip. After dotenv writes we unconditionally chmod
        ``0o600`` because :func:`dotenv.set_key` has been observed
        to loosen permissions on some paths.

        If ``key`` is not in :data:`DOCUMENTED_VARS` we raise
        :class:`ValueError`. The ``allow_unknown=True`` escape hatch
        is for the dashboard's free-form "extra rows" surface so the
        operator can stash a variable the wizard hasn't documented
        yet without having to edit ``.env.example``.

        ``actor`` is reserved for the subtask-12 audit wiring; this
        method intentionally does not import :func:`audit_event`.
        """
        if not allow_unknown and key not in _DOCUMENTED_NAMES:
            raise ValueError(f"unknown env key: {key!r}")
        set_key(str(self._path), key, value, quote_mode="always")
        self._enforce_mode()

    def delete(
        self,
        key: str,
        *,
        actor: str = "operator",
        allow_unknown: bool = False,
    ) -> None:
        """Remove one key, then re-tighten file permissions.

        Same allow-list discipline as :meth:`set`: unknown keys
        raise :class:`ValueError` unless the caller explicitly opts
        out. ``unset_key`` is a no-op if the file or key is missing,
        so we don't need a pre-flight existence check.

        ``actor`` is reserved for subtask 12.
        """
        if not allow_unknown and key not in _DOCUMENTED_NAMES:
            raise ValueError(f"unknown env key: {key!r}")
        unset_key(str(self._path), key)
        self._enforce_mode()

    def documented_vars(self) -> list[DocumentedVar]:
        """Return the canonical documented-variables list.

        Thin accessor so callers (the GET ``/env`` route handler)
        don't have to import :data:`DOCUMENTED_VARS` directly. The
        list is the same module-level constant; returning a fresh
        list copy isn't necessary because :class:`DocumentedVar` is
        frozen and the list itself is treated as read-only by the
        route layer.
        """
        return DOCUMENTED_VARS

    def _enforce_mode(self) -> None:
        """Re-apply ``0o600`` on the dotenv file.

        Called after every successful mutation. Swallowing
        :class:`FileNotFoundError` covers the edge case where dotenv
        decided not to create the file (e.g. ``unset_key`` on a
        missing file) — there's nothing to chmod and the next
        mutation will create it at the correct mode.
        """
        try:
            os.chmod(self._path, 0o600)
        except FileNotFoundError:
            pass
