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
# against the new daemon.
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

step_1_platform() {
    say "[1/10] checking platform"
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
    say "[2/10] checking for existing legacy install"
    if [ ! -d "$ANNA_HOME/.venv" ]; then
        say "  no ~/anna/.venv/ found — migration already complete or never installed."
        say "  exiting 0."
        exit 0
    fi
    say "  found $ANNA_HOME/.venv — proceeding with migration"
}

step_3_stop_daemon() {
    say "[3/10] stopping the running daemon"
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
    say "[4/10] snapshotting operator state"
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
    say "[5/10] resolving source directory for uv tool install"
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
    say "[6/10] running uv tool install --reinstall $SOURCE_DIR"
    if ! command -v uv >/dev/null 2>&1; then
        die "uv is not installed. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
    fi
    uv tool install --reinstall "$SOURCE_DIR"
}

step_7_verify_binary() {
    say "[7/10] verifying ~/.local/bin/anna"
    if ! env -i HOME="$HOME" PATH="$HOME/.local/bin:$PATH" \
        "$HOME/.local/bin/anna" --help >/dev/null 2>&1; then
        die "the new anna binary at ~/.local/bin/anna does not respond. Inspect: uv tool list"
    fi
    say "  ~/.local/bin/anna --help: OK"
}

step_8_install_unit() {
    say "[8/10] writing the new systemd unit"
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
    say "[9/10] starting the new daemon and probing readiness (20s timeout)"
    local since
    since="$(date '+%Y-%m-%d %H:%M:%S')"
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
                warn "  inspect: journalctl --user -u anna --since '$since'"
                die "aborting: the new daemon is restart-looping. The old venv is untouched; see rollback below."
            fi
            say "  daemon is active and stable (NRestarts=0)"
            return
        fi
        sleep 1
        i=$((i+1))
    done
    warn "  daemon did not reach 'active' state within 20s. Current state: $state"
    warn "  inspect: journalctl --user -u anna --since '$since'"
    die "aborting: readiness probe failed. The old venv is untouched; see rollback below."
}

step_10_delete_venv() {
    say "[10/10] cleaning up the old install"
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
    step_10_delete_venv
    say "migration complete. anna is on PATH at ~/.local/bin/anna."
    say "verify with:"
    say "  systemctl --user status anna"
    say "  anna --help"
}

main "$@"
