#!/usr/bin/env bash
#
# migrate-to-uv-tool.sh: migrate a pre-Phase-A ANNA install to the new
# uv-tool-managed shape.
#
# Run once on a host that has the legacy install (~/anna/.venv/ exists
# and the systemd unit points at %h/anna/.venv/bin/anna). After the
# script completes:
#
#   - The anna binary lives at ~/.local/bin/anna (uv-tool-managed).
#   - The systemd unit at ~/.config/systemd/user/anna.service has been
#     rewritten to ExecStart=%h/.local/bin/anna.
#   - ~/anna/.venv/ is gone.
#   - All operator state (anna.yaml, .env, core/, audit/, transcripts/,
#     schedules.yaml) is untouched.
#
# Idempotent: re-running after a successful migration exits 0 fast.
# Safe: every destructive step is gated on a successful readiness probe
# against the new daemon, PLUS a transport-connectivity gate that tails
# journalctl until every transport enabled in anna.yaml has logged its
# "channel.connected" marker. A daemon that is "active" but silently
# degraded to CLI-only (2026-06-02 incident) does not pass.
#
set -euo pipefail

ANNA_HOME="${ANNA_HOME:-$HOME/anna}"
REPO_URL="${ANNA_REPO_URL:-https://github.com/iamfuntime/anna}"

say()  { printf '\033[1;36m[migrate]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[migrate]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[migrate]\033[0m %s\n' "$*" >&2; exit 1; }

confirm() {
    local answer
    printf '\033[1;36m[migrate]\033[0m %s [y/N] ' "$1" >&2
    read -r answer
    case "$answer" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

# Print the transports enabled in the live anna.yaml, space-separated on
# stdout (diagnostics go to stderr via warn so command substitution stays
# clean). Pure bash/awk; defaults mirror src/anna/config.py: slack false,
# telegram false, cli true.
enabled_transports() {
    local yaml="$ANNA_HOME/anna.yaml"
    local slack=false telegram=false cli=true
    if [ ! -r "$yaml" ]; then
        warn "  $yaml not found or unreadable; assuming default transports (cli only)"
    else
        local name val
        while IFS='=' read -r name val; do
            case "$name" in
                slack)    slack="$val" ;;
                telegram) telegram="$val" ;;
                cli)      cli="$val" ;;
            esac
        done < <(awk '
            $0 ~ /^transports:/ { in_block=1; next }
            in_block && $0 ~ /^[^[:space:]]/ { in_block=0 }
            in_block {
                line=$0
                sub(/[[:space:]]*#.*$/, "", line)
                if (line ~ /^[[:space:]]+[A-Za-z0-9_]+:[[:space:]]*$/) {
                    key=line; gsub(/[[:space:]:]/, "", key); cur=key
                } else if (cur != "" && line ~ /^[[:space:]]+enabled:/) {
                    val=line
                    sub(/^[[:space:]]+enabled:[[:space:]]*/, "", val)
                    gsub(/["[:space:]]/, "", val)
                    gsub(/\047/, "", val)
                    printf "%s=%s\n", cur, tolower(val)
                }
            }
        ' "$yaml")
    fi
    local out=""
    [ "$slack" = "true" ]    && out="$out slack"
    [ "$telegram" = "true" ] && out="$out telegram"
    [ "$cli" = "true" ]      && out="$out cli"
    printf '%s\n' "${out# }"
}

# COUPLING: the two helpers below grep journalctl MESSAGE lines (structlog
# JSON on stdout) for exact event strings emitted by the daemon:
#
#   "channel.connected"               src/anna/transports/{slack,telegram,cli}.py
#   "channel.token_missing"           src/anna/transports/{slack,telegram}.py
#   "audit.transport.token_missing"   src/anna/transports/{slack,telegram}.py
#
# tests/test_migration_log_markers.py pins these strings against the Python
# sources — rename a marker there and this gate (and CI) must change with it.

# journal_has_connected <journal text> <transport>: did <transport> log its
# channel.connected marker?
journal_has_connected() {
    printf '%s\n' "$1" \
        | grep -F '"channel.connected"' \
        | grep -Eq "\"channel\": ?\"$2\""
}

# journal_has_token_missing <journal text> <transport>: did <transport> boot
# tokenless (WARNING and/or audit mirror)?
journal_has_token_missing() {
    printf '%s\n' "$1" \
        | grep -E '"(channel\.token_missing|audit\.transport\.token_missing)"' \
        | grep -Eq "\"channel\": ?\"$2\""
}

transport_gate_failed() {
    local transports="$1" reason="$2"
    warn ""
    warn "============================================================"
    warn "  FAILED: transport connectivity gate"
    warn "  transport(s) that never connected:$(printf ' %s' $transports)"
    warn "  reason: $reason"
    warn ""
    warn "  The daemon is 'active' but is NOT serving every transport"
    warn "  enabled in $ANNA_HOME/anna.yaml. A silent CLI-only"
    warn "  degradation (2026-06-02 incident) must not pass migration."
    warn ""
    warn "  inspect:  journalctl --user -u anna --since '$PROBE_SINCE'"
    warn "  rollback: systemctl --user stop anna.service, restore the"
    warn "            snapshot from ~/.cache/anna-migration-snapshot-*.tgz,"
    warn "            and point the unit back at $ANNA_HOME/.venv/bin/anna."
    warn "============================================================"
    die "aborting: enabled transport(s) never connected:$(printf ' %s' $transports). The old venv is untouched."
}

step_1_platform() {
    say "[1/11] checking platform"
    local kernel
    kernel="$(uname -s)"
    if [ "$kernel" = "Darwin" ]; then
        die "no macOS legacy installs exist to migrate. macOS operators should use install.sh on a fresh host."
    fi
    if [ "$kernel" != "Linux" ]; then
        die "unsupported platform: $kernel (supported: Linux)"
    fi
    if [ -r /proc/version ] && grep -qi microsoft /proc/version; then
        say "  detected WSL2; Linux flow applies"
    fi
}

step_2_already_migrated() {
    say "[2/11] checking for existing legacy install"
    if [ ! -d "$ANNA_HOME/.venv" ]; then
        say "  no ~/anna/.venv/ found — migration already complete or never installed."
        say "  exiting 0."
        exit 0
    fi
    say "  found $ANNA_HOME/.venv — proceeding with migration"
}

step_3_stop_daemon() {
    say "[3/11] stopping the running daemon"
    if ! command -v systemctl >/dev/null 2>&1; then
        warn "  systemctl not found; skipping daemon stop"
        return
    fi
    if systemctl --user is-active --quiet anna.service; then
        systemctl --user stop anna.service
        say "  systemctl --user stop anna.service: OK"
    else
        say "  anna.service was not active; nothing to stop"
    fi
}

step_4_snapshot() {
    say "[4/11] snapshotting operator state"
    local ts snap
    ts="$(date +%Y%m%d-%H%M%S)"
    snap="$HOME/.cache/anna-migration-snapshot-$ts.tgz"
    mkdir -p "$HOME/.cache"
    tar czf "$snap" \
        --exclude="$ANNA_HOME/.venv" \
        --exclude="$ANNA_HOME/src" \
        --exclude="$ANNA_HOME/.git" \
        --exclude="$ANNA_HOME/tests" \
        --exclude="$ANNA_HOME/__pycache__" \
        -C "$HOME" \
        "$(basename "$ANNA_HOME")" 2>/dev/null || true
    if [ -f "$snap" ]; then
        say "  snapshot written to $snap"
    else
        warn "  snapshot failed (tar exit nonzero); continuing without it"
    fi
}

step_5_resolve_source() {
    say "[5/11] resolving source directory for uv tool install"
    if [ -n "${ANNA_SOURCE_DIR:-}" ] && [ -d "$ANNA_SOURCE_DIR" ]; then
        SOURCE_DIR="$ANNA_SOURCE_DIR"
        say "  using \$ANNA_SOURCE_DIR: $SOURCE_DIR"
        return
    fi
    if [ -d "$HOME/git/anna/.git" ]; then
        SOURCE_DIR="$HOME/git/anna"
        say "  using local dev checkout: $SOURCE_DIR"
        return
    fi
    local cache="$HOME/.cache/anna-source"
    if [ ! -d "$cache/.git" ]; then
        say "  cloning $REPO_URL into $cache"
        rm -rf "$cache"
        git clone --quiet "$REPO_URL" "$cache"
    else
        say "  updating cached source at $cache"
        git -C "$cache" fetch --quiet --prune
        git -C "$cache" reset --hard --quiet origin/main
    fi
    SOURCE_DIR="$cache"
}

step_6_uv_install() {
    say "[6/11] running uv tool install --reinstall $SOURCE_DIR"
    if ! command -v uv >/dev/null 2>&1; then
        die "uv is not installed. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
    fi
    uv tool install --reinstall "$SOURCE_DIR"
}

step_7_verify_binary() {
    say "[7/11] verifying ~/.local/bin/anna"
    if ! env -i HOME="$HOME" PATH="$HOME/.local/bin:$PATH" \
        "$HOME/.local/bin/anna" --help >/dev/null 2>&1; then
        die "the new anna binary at ~/.local/bin/anna does not respond. Inspect: uv tool list"
    fi
    say "  ~/.local/bin/anna --help: OK"
}

step_8_install_unit() {
    say "[8/11] writing the new systemd unit"
    mkdir -p "$HOME/.config/systemd/user"
    uv tool run --from anna python -c \
        "from importlib.resources import files; \
         print(files('anna.setup.templates').joinpath('anna.service').read_text(), end='')" \
        > "$HOME/.config/systemd/user/anna.service.new"
    mv "$HOME/.config/systemd/user/anna.service.new" "$HOME/.config/systemd/user/anna.service"
    systemctl --user daemon-reload
    say "  new unit written and daemon-reload completed"
}

step_9_restart_probe() {
    say "[9/11] starting the new daemon and probing readiness (20s timeout)"
    # PROBE_SINCE is global: step_10_transport_probe tails the journal from
    # the same instant so it only sees this boot's markers.
    PROBE_SINCE="$(date '+%Y-%m-%d %H:%M:%S')"
    systemctl --user start anna.service

    local i=0
    while [ $i -lt 20 ]; do
        local state
        state="$(systemctl --user is-active anna.service 2>/dev/null || true)"
        if [ "$state" = "active" ]; then
            local restarts
            restarts="$(systemctl --user show -p NRestarts --value anna.service)"
            if [ "${restarts:-0}" -gt 0 ]; then
                warn "  daemon is active but has restarted ${restarts}x in this session"
                warn "  inspect: journalctl --user -u anna --since '$PROBE_SINCE'"
                die "aborting: the new daemon is restart-looping. The old venv is untouched; see rollback below."
            fi
            say "  daemon is active and stable (NRestarts=0)"
            return
        fi
        sleep 1
        i=$((i+1))
    done
    warn "  daemon did not reach 'active' state within 20s. Current state: $state"
    warn "  inspect: journalctl --user -u anna --since '$PROBE_SINCE'"
    die "aborting: readiness probe failed. The old venv is untouched; see rollback below."
}

step_10_transport_probe() {
    say "[10/11] verifying transport connectivity via journal markers (30s timeout)"
    if ! command -v journalctl >/dev/null 2>&1; then
        die "journalctl not found; cannot verify transport connectivity. The old venv is untouched; see rollback below."
    fi

    local expected
    expected="$(enabled_transports)"
    if [ -z "$expected" ]; then
        warn "  no transports enabled in $ANNA_HOME/anna.yaml; nothing to verify"
        return
    fi
    say "  enabled transports (from $ANNA_HOME/anna.yaml):$(printf ' %s' $expected)"

    # An "active" unit is necessary but not sufficient: a transport that is
    # enabled in anna.yaml but never connects (e.g. missing token) leaves the
    # daemon healthy-looking while silently degraded. Wait for each enabled
    # transport's channel.connected marker; fail fast on token_missing.
    local pending="$expected"
    local deadline=$((SECONDS + 30))
    while :; do
        local logs t still=""
        logs="$(journalctl --user -u anna -o cat --no-pager --since "$PROBE_SINCE" 2>/dev/null || true)"
        for t in $pending; do
            if journal_has_token_missing "$logs" "$t"; then
                transport_gate_failed "$t" "the daemon reported token_missing for '$t' — its token env var is not set in the unit's environment"
            fi
            if journal_has_connected "$logs" "$t"; then
                say "  $t: channel.connected"
            else
                still="$still $t"
            fi
        done
        pending="${still# }"
        if [ -z "$pending" ]; then
            say "  all enabled transports connected"
            return
        fi
        if [ $SECONDS -ge $deadline ]; then
            break
        fi
        sleep 1
    done
    transport_gate_failed "$pending" "no channel.connected marker within 30s"
}

step_11_delete_venv() {
    say "[11/11] cleaning up the old install"
    say "  the new daemon is healthy. Ready to delete the legacy install artifacts:"
    say "    - $ANNA_HOME/.venv/    (the old virtualenv)"
    say "    - $ANNA_HOME/src/      (source tree)"
    say "    - $ANNA_HOME/tests/    (test suite)"
    say "    - $ANNA_HOME/.git/     (git checkout)"
    say "    - $ANNA_HOME/pyproject.toml, uv.lock, install.sh, README.md"
    say "  Operator state files (anna.yaml, .env, core/, audit/, transcripts/, schedules.yaml,"
    say "  agents/, skills/, anna.sock, state/) will NOT be touched."
    if ! confirm "Delete the legacy artifacts listed above?"; then
        warn "  skipped cleanup. ANNA is running on the new install but ~/anna/.venv still exists."
        warn "  delete manually when ready."
        return
    fi
    rm -rf "$ANNA_HOME/.venv"
    rm -rf "$ANNA_HOME/src"
    rm -rf "$ANNA_HOME/tests"
    rm -rf "$ANNA_HOME/.git"
    rm -f  "$ANNA_HOME/pyproject.toml" "$ANNA_HOME/uv.lock" "$ANNA_HOME/install.sh" "$ANNA_HOME/README.md"
    rm -rf "$ANNA_HOME/scripts"
    say "  legacy artifacts removed. $ANNA_HOME is now state-only."
}

main() {
    say "ANNA migration to uv-tool-managed install"
    step_1_platform
    step_2_already_migrated
    step_3_stop_daemon
    step_4_snapshot
    step_5_resolve_source
    step_6_uv_install
    step_7_verify_binary
    step_8_install_unit
    step_9_restart_probe
    step_10_transport_probe
    step_11_delete_venv
    say "migration complete. anna is on PATH at ~/.local/bin/anna."
    say "verify with:"
    say "  systemctl --user status anna"
    say "  anna --help"
}

# Guarded so tests can `source` this file and exercise the helper functions
# (enabled_transports, journal_has_connected, ...) without running a migration.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
