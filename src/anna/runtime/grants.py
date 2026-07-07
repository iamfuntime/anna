"""Per-agent permission resolution + ANNA-native MCP registry binding.

This module turns the hybrid POLICY / GRANT config (see config.py) into a
single concrete :class:`ResolvedGrant` for a delegation, then binds that
grant's MCP specs into the SDK's ``mcp_servers`` dict + ``allowed_tools``
additions.

Security model recap
--------------------
* POLICY (operator-only, restart-gated, in anna.yaml): ``subagents.dir_pool``
  (name -> abs path) and ``subagents.mcp_registry`` (name -> McpServerSpec).
  The ``anna_self_edit`` MCP cannot rewrite anna.yaml, so these are trusted.
* GRANTS (untrusted): ``subagents.agents.<slug>`` and persona frontmatter
  ``grants:``. They may ONLY reference pool / registry names. An unknown name
  is DROPPED + logged WARNING, never invented — so the reachable set is always
  a subset of what the operator blessed.
* The forbidden builtins ``anna_self_edit`` / ``anna_google`` / ``anna_delegate``
  are structurally unreachable: they are not in ``_BUILTIN_FACTORIES`` and a
  registry entry naming one is dropped at MCP-build time.

SDK confirmation spike (subtask 1, verified against the bundled
``claude_agent_sdk`` in .venv on 2026-06-03)
---------------------------------------------------------------
(a) ``ClaudeAgentOptions.mcp_servers`` is typed ``dict[str, McpServerConfig]``
    where ``McpServerConfig = McpStdioServerConfig | McpSSEServerConfig |
    McpHttpServerConfig | McpSdkServerConfig`` (types.py ~635). So literal
    stdio/http dicts are accepted alongside the ``{"type": "sdk", ...}`` dict
    that ``create_sdk_mcp_server`` returns. Stdio shape:
    ``{"command": str, "args"?: [...], "env"?: {...}}`` (``type`` optional,
    defaults to stdio). HTTP shape: ``{"type": "http", "url": str,
    "headers"?: {...}}``.
(b) The server-namespace wildcard ``mcp__<server>__*`` IS honored for external
    tools. The bundled CLI binary builds ``mcp__${serverName}__*`` dynamically
    and its own permission-rule validator suggests it "for all tools" of a
    server. So external servers contribute ``mcp__<name>__*`` to
    ``allowed_tools`` by default; ``McpServerSpec.tool_names`` (shipped as an
    empty list either way) lets an operator pin the surface to named tools,
    in which case the resolver expands ``mcp__<name>__<tool>`` explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anna.config import AgentGrants, AnnaConfig, McpServerSpec
from anna.log import get_logger

_log = get_logger("anna.grants")

# Builtin MCP servers a registry entry / fallback may resolve to. The
# forbidden trio (anna_self_edit / anna_google / anna_delegate) is
# deliberately ABSENT so that no grant — operator or untrusted — can route a
# sub-agent back to self-edit, mail, or further delegation. build_mcp_servers
# drops + logs any registry entry whose builtin_name is not a key here.
_ALLOWED_BUILTINS: frozenset[str] = frozenset({"anna_web"})

# The fallback MCP builtin name used today when tools are enabled and no
# explicit grant narrows the surface.
_FALLBACK_BUILTIN = "anna_web"

# Reserved in-process builtin names that must NEVER be sub-agent-mountable:
# self-edit, mail, and further delegation. They are deliberately absent from
# ``_ALLOWED_BUILTINS`` (so a registry ``builtin`` naming one is dropped at
# build time) AND rejected as a registry KEY at config-load (see
# SubagentsConfig in config.py, which imports this set as the single source of
# truth — avoid drift).
FORBIDDEN_BUILTINS: frozenset[str] = frozenset(
    {"anna_self_edit", "anna_google", "anna_delegate"}
)


@dataclass
class ResolvedGrant:
    """Concrete, name-resolved capability set for a single delegation.

    Produced by :func:`resolve_effective_grant`. All names have already been
    resolved against the operator pools; unknown names are gone.

    Attributes:
        write_dirs: Absolute, ``~``-expanded directories to mount into the
            sub-agent's ``add_dirs``.
        mcp_specs: ``(name, spec)`` pairs to bind via
            :func:`build_mcp_servers`. Order is stable (resolution order).
        allowed_tools: The tool allow-list to seed the sub-agent's
            ``allowed_tools`` with (MCP tool additions are appended later by
            :func:`build_mcp_servers`).
        permission_mode: SDK permission mode for the delegation.
        model: Claude model for the delegation, or ``None`` to inherit the
            CLI/account default. Free-form (tier alias or full ID); NOT
            resolved against an operator pool — a model choice cannot escalate
            capability, so there is no clamp (contrast ``permission_mode``).
        effort: Reasoning-effort level for the delegation
            (low|medium|high|xhigh|max), or ``None`` to fall through to the
            SDK default ("high"). Unlike ``model``, the fallback layer does
            NOT seed this from ``runtime.effort`` — sub-agents deliberately
            do not inherit the main loop's effort. Free-form like ``model``:
            no capability-escalation risk, so no clamp.
    """

    write_dirs: list[str] = field(default_factory=list)
    mcp_specs: list[tuple[str, McpServerSpec]] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    permission_mode: str = "acceptEdits"
    model: str | None = None
    effort: str | None = None


@dataclass
class _GrantLayer:
    """A single precedence layer, pre-name-resolution.

    Mirrors :class:`AgentGrants` but every field is optional via ``None`` to
    express "this layer does not specify the field" (pass-through) distinctly
    from "this layer specifies the empty list" (REPLACE with nothing).
    """

    write_dirs: list[str] | None = None
    mcp_servers: list[str] | None = None
    allowed_tools: list[str] | None = None
    permission_mode: str | None = None
    model: str | None = None
    effort: str | None = None


def _layer_from_grants(grants: AgentGrants | None) -> _GrantLayer:
    """Convert an :class:`AgentGrants` into a precedence layer.

    The pydantic model defaults its list fields to ``[]``, which would read
    as "REPLACE with nothing" on every layer and clobber the fallback. We
    cannot distinguish "operator wrote ``write_dirs: []``" from "operator
    omitted write_dirs" once pydantic has filled the default. The product
    decision (subtask 5): a grant layer's list field counts as *specified*
    only when it is non-empty; an empty list reads as absence (pass-through).
    The optional scalars (``allowed_tools``, ``permission_mode``) keep their
    ``None``-means-absent semantics natively.
    """
    if grants is None:
        return _GrantLayer()
    return _GrantLayer(
        write_dirs=list(grants.write_dirs) if grants.write_dirs else None,
        mcp_servers=list(grants.mcp_servers) if grants.mcp_servers else None,
        allowed_tools=(
            list(grants.allowed_tools)
            if grants.allowed_tools is not None
            else None
        ),
        permission_mode=grants.permission_mode,
        model=grants.model,
        effort=grants.effort,
    )


def _fallback_layer(config: AnnaConfig) -> _GrantLayer:
    """Layer 1: today's global-fallback behavior.

    * write_dirs <- ``subagents.extra_dirs`` (already abs paths / ``~`` forms)
    * mcp_servers <- the builtin ``anna_web`` when ``tools.enabled``, else none
    * allowed_tools <- ``subagents.allowed_tools``
    * permission_mode <- ``"acceptEdits"`` (the sub-agent default today)
    * model <- ``config.runtime.model`` (the global default; ``None`` =
      inherit the CLI/account default, today's behavior). This is what makes
      every override-less sub-agent inherit the main loop's model.
    * effort <- ``None``, deliberately NOT ``config.runtime.effort`` — the
      main loop's effort setting is for the main loop only. An override-less
      sub-agent falls through to the SDK default ("high"); only the
      per-agent yaml layer or persona frontmatter can set it.
    """
    mcp = [_FALLBACK_BUILTIN] if config.tools.enabled else []
    return _GrantLayer(
        write_dirs=list(config.subagents.extra_dirs) or None,
        mcp_servers=mcp or None,
        allowed_tools=list(config.subagents.allowed_tools),
        permission_mode="acceptEdits",
        model=config.runtime.model,
        effort=None,
    )


def _merge(lower: _GrantLayer, higher: _GrantLayer) -> _GrantLayer:
    """Per-field REPLACE: ``higher`` wins where it specifies a field."""
    return _GrantLayer(
        write_dirs=(
            higher.write_dirs
            if higher.write_dirs is not None
            else lower.write_dirs
        ),
        mcp_servers=(
            higher.mcp_servers
            if higher.mcp_servers is not None
            else lower.mcp_servers
        ),
        allowed_tools=(
            higher.allowed_tools
            if higher.allowed_tools is not None
            else lower.allowed_tools
        ),
        permission_mode=(
            higher.permission_mode
            if higher.permission_mode is not None
            else lower.permission_mode
        ),
        model=(
            higher.model
            if higher.model is not None
            else lower.model
        ),
        effort=(
            higher.effort
            if higher.effort is not None
            else lower.effort
        ),
    )


def _resolve_builtin_fallback_spec(name: str) -> McpServerSpec:
    """Synthesize the McpServerSpec for the implicit fallback builtin."""
    return McpServerSpec(kind="builtin", builtin_name=name)


def resolve_effective_grant(
    config: AnnaConfig,
    slug: str,
    frontmatter_grants: AgentGrants | None,
) -> ResolvedGrant:
    """Resolve the effective grant for ``slug`` across the three layers.

    Precedence (lists REPLACE, not union; higher layer wins per field):

      1. global fallback (today's behavior)
      2. ``config.subagents.agents.get(slug)``
      3. ``frontmatter_grants`` (from the persona file)

    After merge, names are resolved against the operator pools:

      * each ``write_dirs`` name -> ``config.subagents.dir_pool[name]``
        (``~``-expanded). Unknown name -> drop + WARNING.
      * each ``mcp_servers`` name -> ``config.subagents.mcp_registry[name]``,
        except the implicit fallback ``anna_web`` which synthesizes a builtin
        spec. Unknown name -> drop + WARNING.

    Note this is *name* resolution only — the forbidden-builtin drop and the
    actual SDK binding happen in :func:`build_mcp_servers`.
    """
    # Sanitize the UNTRUSTED layer-3 (frontmatter) source BEFORE merging:
    # persona frontmatter is rewritable by anna_self_edit and must not be able
    # to escalate posture by removing write-gating. Clamp ONLY
    # ``bypassPermissions`` (the sole mode that drops gating) to unset for the
    # merge; ``default``/``acceptEdits``/``plan`` pass through. The TRUSTED
    # layer-2 (``config.subagents.agents[slug]``, operator-written in
    # anna.yaml) is untouched and may still set any valid mode. Provenance is
    # unambiguous: ``frontmatter_grants`` is a distinct parameter. We do not
    # mutate the caller's object — a local override layer carries the clamp.
    fm_layer = _layer_from_grants(frontmatter_grants)
    if (
        frontmatter_grants is not None
        and frontmatter_grants.permission_mode == "bypassPermissions"
    ):
        _log.warning(
            "grants.permission_mode.frontmatter_escalation_denied",
            slug=slug,
            requested="bypassPermissions",
        )
        fm_layer.permission_mode = None

    merged = _merge(_fallback_layer(config), _layer_from_grants(config.subagents.agents.get(slug)))
    merged = _merge(merged, fm_layer)

    # ``subagents.extra_dirs`` are LITERAL paths the operator wrote directly in
    # anna.yaml (not pool names), so they can be mounted verbatim instead of
    # looked up in dir_pool — preserving the pre-chunk-A behavior where
    # extra_dirs mounted straight into add_dirs. Safety here comes from the
    # SOURCE of ``fallback_literals``, NOT from REPLACE semantics: every member
    # is an operator-blessed ``extra_dirs`` entry (the same operator-only trust
    # tier as dir_pool). The literal-passthrough branch below only ever waves
    # through a name that is already in this operator-blessed set; it does not
    # rely on REPLACE having stripped any grant-origin name from write_dirs. A
    # grant-origin name that does not resolve via dir_pool and is not in this
    # set is dropped + logged like any other unknown name.
    fallback_literals = set(config.subagents.extra_dirs)

    # --- write_dirs name resolution ---
    resolved_dirs: list[str] = []
    for name in merged.write_dirs or []:
        raw = config.subagents.dir_pool.get(name)
        if raw is None and name in fallback_literals:
            # Literal fallback path (operator-written extra_dir).
            resolved_dirs.append(str(Path(name).expanduser()))
            continue
        if raw is None:
            _log.warning(
                "grants.dir_pool.unknown",
                slug=slug,
                dropped_name=name,
                kind="write_dir",
            )
            continue
        resolved_dirs.append(str(Path(raw).expanduser()))

    # --- mcp_servers name resolution ---
    resolved_specs: list[tuple[str, McpServerSpec]] = []
    for name in merged.mcp_servers or []:
        if name == _FALLBACK_BUILTIN and name not in config.subagents.mcp_registry:
            # Implicit fallback builtin — synthesize its spec.
            resolved_specs.append((name, _resolve_builtin_fallback_spec(name)))
            continue
        spec = config.subagents.mcp_registry.get(name)
        if spec is None:
            _log.warning(
                "grants.mcp_registry.unknown",
                slug=slug,
                dropped_name=name,
                kind="mcp_server",
            )
            continue
        resolved_specs.append((name, spec))

    return ResolvedGrant(
        write_dirs=resolved_dirs,
        mcp_specs=resolved_specs,
        allowed_tools=list(merged.allowed_tools or []),
        permission_mode=merged.permission_mode or "acceptEdits",
        # ``model`` is free-form: passed through verbatim, NOT resolved against
        # an operator pool. A model choice carries no capability-escalation
        # risk (the grant security model gates dir/server reachability, not
        # which model executes), so there is no clamp like the bypassPermissions
        # one above. ``None`` = inherit the CLI/account default.
        model=merged.model,
        # ``effort`` is free-form like ``model`` (no pool, no clamp). The
        # fallback layer seeds None, so this is non-None only when the
        # per-agent yaml or persona frontmatter set it; None falls through
        # to the SDK default ("high") at the ClaudeAgentOptions boundary.
        effort=merged.effort,
    )


# ---------------------------------------------------------------------------
# Subtask 6 — MCP resolver
# ---------------------------------------------------------------------------


def build_mcp_servers(
    config: AnnaConfig,
    resolved_specs: list[tuple[str, McpServerSpec]],
    conv_key: str,
) -> tuple[dict[str, Any], list[str]]:
    """Bind resolved MCP specs into the SDK ``mcp_servers`` dict + tool additions.

    Args:
        config: Live config (for the builtin factories' dependencies).
        resolved_specs: ``(name, spec)`` pairs from a :class:`ResolvedGrant`.
        conv_key: Synthetic conv_key threaded into builtin factory closures so
            tool calls fired from inside the sub-agent get audit-stamped.

    Returns:
        ``(mcp_servers, tool_additions)`` where ``mcp_servers`` is the dict to
        hand to ``ClaudeAgentOptions.mcp_servers`` and ``tool_additions`` is
        the list of tool names to extend ``allowed_tools`` with.

    Drops (with a WARNING):

      * a ``builtin`` whose ``builtin_name`` is not in ``_ALLOWED_BUILTINS``
        (the forbidden trio + any typo), or whose factory returns ``None``
        (e.g. ``tools.enabled`` is false).
    """
    mcp_servers: dict[str, Any] = {}
    tool_additions: list[str] = []

    for name, spec in resolved_specs:
        if spec.kind == "builtin":
            built = _build_builtin(config, name, spec, conv_key)
            if built is None:
                continue
            server, names = built
            mcp_servers[name] = server
            tool_additions.extend(names)
        elif spec.kind == "stdio":
            server_dict: dict[str, Any] = {
                "type": "stdio",
                "command": spec.command,
            }
            if spec.args:
                server_dict["args"] = list(spec.args)
            if spec.env:
                server_dict["env"] = dict(spec.env)
            mcp_servers[name] = server_dict
            tool_additions.extend(_external_tool_names(name, spec))
        elif spec.kind == "http":
            http_dict: dict[str, Any] = {
                "type": "http",
                "url": spec.url,
            }
            if spec.headers:
                http_dict["headers"] = dict(spec.headers)
            mcp_servers[name] = http_dict
            tool_additions.extend(_external_tool_names(name, spec))

    return mcp_servers, tool_additions


def _external_tool_names(name: str, spec: McpServerSpec) -> list[str]:
    """Tool-allow-list additions for an external (stdio/http) server.

    Per subtask-1: the bundled CLI honors the server-namespace wildcard, so
    with no explicit ``tool_names`` we contribute ``mcp__<name>__*`` (all
    tools). When ``tool_names`` is set, we pin the surface to the named tools
    as ``mcp__<name>__<tool>``.
    """
    if spec.tool_names:
        return [f"mcp__{name}__{tool}" for tool in spec.tool_names]
    return [f"mcp__{name}__*"]


def _build_builtin(
    config: AnnaConfig,
    name: str,
    spec: McpServerSpec,
    conv_key: str,
) -> tuple[Any, list[str]] | None:
    """Construct a builtin MCP server + its tool-name additions, or ``None``.

    Returns ``None`` (and logs) when the builtin is forbidden / unknown, or
    when its factory declines to build (e.g. tools disabled).
    """
    builtin_name = spec.builtin_name or ""
    if builtin_name not in _ALLOWED_BUILTINS:
        _log.warning(
            "grants.builtin.forbidden",
            registry_name=name,
            dropped_name=builtin_name,
            kind="builtin",
        )
        return None

    if builtin_name == "anna_web":
        from anna.tools.vault_tools import VaultTools
        from anna.tools.web_server import WEB_TOOL_NAMES, build_web_server
        from anna.tools.web_tools import WebTools

        server = build_web_server(
            config=config,
            web_tools=WebTools(config=config),
            vault_tools=VaultTools(config=config),
            conv_key=conv_key,
        )
        if server is None:
            # tools.enabled is false — nothing to mount.
            return None
        tool_names = [f"mcp__{name}__{tool}" for tool in WEB_TOOL_NAMES]
        return server, tool_names

    # Defensive: a name in _ALLOWED_BUILTINS without a dispatch arm.
    _log.warning(
        "grants.builtin.no_factory",
        registry_name=name,
        dropped_name=builtin_name,
        kind="builtin",
    )
    return None
