#!/usr/bin/env bash
#
# install.sh: ANNA one-line installer (Linux, Phase A).
#
#   curl -fsSL https://anna.funtime.dev/install.sh | bash
#
# Installs ANNA via `uv tool install` into a managed venv under
# ~/.local/share/uv/tools/anna/, with shim binaries dropped into
# ~/.local/bin/. ~/anna/ becomes state-only — no source, no venv.
#
set -euo pipefail

ANNA_HOME="${ANNA_HOME:-$HOME/anna}"
REPO_URL="${ANNA_REPO_URL:-https://github.com/iamfuntime/anna}"
ANNA_SOURCE_DIR="${ANNA_SOURCE_DIR:-}"      # set by Makefile dev loop

# Colored banners help the operator track progress when running this
# through curl-pipe-bash where the terminal scrolls quickly.
say()  { printf '\033[1;36m[anna]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[anna]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[anna]\033[0m %s\n' "$*" >&2; exit 1; }

check_prereq() {
    local name="$1" cmd="$2" hint="$3"
    command -v "$cmd" >/dev/null 2>&1 && return 0
    warn "missing prerequisite: $name"
    [ -n "$hint" ] && warn "  install it with: $hint"
    die "install $name and re-run this script."
}

check_uv() {
    # uv is the only Python toolchain the new install model uses. We don't
    # auto-bootstrap it because the upstream installer wants its own
    # informed consent (PATH edits, ~/.local/share writes). Direct the
    # operator to the canonical one-liner and bail.
    if ! command -v uv >/dev/null 2>&1; then
        warn "uv is not installed."
        warn "  install it with:"
        warn "    curl -LsSf https://astral.sh/uv/install.sh | sh"
        die "install uv and re-run this script."
    fi
    say "uv $(uv --version | awk '{print $2}') detected"
}

check_collision() {
    # Refuse to run on top of a pre-Phase-A install — the operator must
    # explicitly run the migration script so they understand the new
    # layout (~/anna/.venv goes away, binaries move to ~/.local/bin).
    if [ -d "$HOME/anna/.venv" ]; then
        die "found existing venv at ~/anna/.venv (legacy install layout). Run scripts/migrate-to-uv-tool.sh from a source checkout to migrate, or delete ~/anna/.venv/ manually if you've already migrated."
    fi
}

warn_path() {
    # The uv tool install drops binaries into ~/.local/bin; many distros
    # don't have that on PATH by default. We can't fix the operator's rc
    # file from inside a curl-pipe-bash, so print the exact line they
    # need to add and continue — `exec` at the end falls back to the
    # absolute path so the wizard still launches.
    case ":$PATH:" in
        *":$HOME/.local/bin:"*)
            say "~/.local/bin is on \$PATH"
            ;;
        *)
            warn "~/.local/bin is NOT on \$PATH."
            warn "After install, the anna binary will live at ~/.local/bin/anna."
            warn "Add this to your shell rc to find it:"
            case "$SHELL" in
                */fish) warn "  fish_add_path ~/.local/bin" ;;
                */zsh)  warn "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc" ;;
                */bash) warn "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc" ;;
                *)      warn "  add ~/.local/bin to PATH in your shell startup file" ;;
            esac
            warn "Then open a new shell, or 'source' the rc file."
            ;;
    esac
}

resolve_source_dir() {
    # Three-way: explicit env var, dev checkout under ~/git/anna,
    # else clone to a transient cache. The Makefile's dev-restart loop
    # sets ANNA_SOURCE_DIR; curl-pipe-bash takes the cache branch.
    if [ -n "$ANNA_SOURCE_DIR" ] && [ -d "$ANNA_SOURCE_DIR" ]; then
        say "using source dir from \$ANNA_SOURCE_DIR: $ANNA_SOURCE_DIR"
        echo "$ANNA_SOURCE_DIR"
        return
    fi
    if [ -d "$HOME/git/anna/.git" ]; then
        say "using local dev checkout: $HOME/git/anna"
        echo "$HOME/git/anna"
        return
    fi
    local cache="$HOME/.cache/anna-source"
    if [ -d "$cache/.git" ]; then
        say "updating cached source clone at $cache"
        git -C "$cache" fetch --quiet --prune
        git -C "$cache" reset --hard --quiet origin/main
    else
        say "fresh install: cloning $REPO_URL into $cache"
        rm -rf "$cache"
        git clone --quiet "$REPO_URL" "$cache"
    fi
    echo "$cache"
}

main() {
    say "checking prerequisites"
    check_prereq "git"   "git"   "apt install git"
    check_prereq "curl"  "curl"  "apt install curl"
    check_uv
    check_collision
    warn_path

    say "preparing source tree"
    local src
    src="$(resolve_source_dir)"

    say "installing anna via uv tool install"
    # --reinstall makes the call idempotent on existing installs.
    uv tool install --reinstall "$src"

    # Verify the shim is reachable from a fresh-env shell, not just the
    # current one. Operator might have just been told PATH is wrong.
    if ! env -i HOME="$HOME" PATH="$HOME/.local/bin:$PATH" "$HOME/.local/bin/anna" --help >/dev/null 2>&1; then
        die "anna binary did not install correctly to ~/.local/bin/anna. Inspect with: uv tool list"
    fi
    say "anna binary installed at ~/.local/bin/anna"

    # Seed the markdown vault. Now invoked from the transient clone, not
    # from $ANNA_HOME — the new install model means $ANNA_HOME is
    # state-only and has no scripts/ directory.
    if [ -x "$src/scripts/seed_vault.sh" ]; then
        say "seeding markdown vault"
        ANNA_HOME="$ANNA_HOME" bash "$src/scripts/seed_vault.sh" || \
            warn "vault seed step failed; continuing — re-run scripts/seed_vault.sh manually if needed"
    fi

    say "handing off to the setup wizard"
    # exec into the shim so this script's bash process is replaced. If
    # PATH doesn't include ~/.local/bin yet, fall back to the absolute path.
    if command -v anna-setup >/dev/null 2>&1; then
        exec anna-setup
    else
        exec "$HOME/.local/bin/anna-setup"
    fi
}

main "$@"
