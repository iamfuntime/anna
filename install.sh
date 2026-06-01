#!/usr/bin/env bash
#
# install.sh: ANNA one-line installer.
#
# Intended invocation, run by the operator from their terminal:
#
#   curl -fsSL https://anna.funtime.dev/install.sh | bash
#
# The script checks prerequisites, clones the repository to $ANNA_HOME
# (default ~/anna), creates a venv, installs the package in editable
# mode, and hands off to the interactive setup wizard.
#
# This file is a TEMPLATE the operator runs. Do not execute it during
# repository scaffolding; it requires network access and writes to the
# operator's home directory.
#
set -euo pipefail

ANNA_HOME="${ANNA_HOME:-$HOME/anna}"
REPO_URL="${ANNA_REPO_URL:-https://github.com/iamfuntime/anna}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Colored banners help the operator track progress when running this
# through curl-pipe-bash where the terminal scrolls quickly.
say() { printf '\033[1;36m[anna]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[anna]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[anna]\033[0m %s\n' "$*" >&2; exit 1; }

check_prereq() {
    local name="$1"
    local cmd="$2"
    local hint="$3"
    command -v "$cmd" >/dev/null 2>&1 && return 0
    warn "missing prerequisite: $name"
    [ -n "$hint" ] && warn "  install it with: $hint"
    die "install $name and re-run this script."
}

check_python_version() {
    local version
    version=$("$PYTHON_BIN" -c 'import sys; print("{}.{}".format(*sys.version_info[:2]))')
    case "$version" in
        3.11|3.12|3.13|3.14|3.15)
            say "python $version detected"
            ;;
        *)
            warn "ANNA requires Python 3.11 or newer, found $version."
            die "install a newer Python (your package manager, pyenv, or python.org) and re-run."
            ;;
    esac
}

main() {
    say "checking prerequisites"
    check_prereq "git" "git" "apt install git  /  brew install git"
    check_prereq "curl" "curl" "apt install curl  /  brew install curl"
    check_prereq "python3" "$PYTHON_BIN" "apt install python3 python3-venv  /  brew install python"
    check_python_version

    if [ -d "$ANNA_HOME/.git" ]; then
        say "updating existing install at $ANNA_HOME"
        # Fast-forward only; never rewrite the operator's history. Runtime
        # artifacts (.env, anna.yaml, core/, vault/, audit/, transcripts/) are
        # gitignored at the repo root, so a pull won't touch them. If the
        # operator hand-edited a *tracked* file the ff fails — warn and keep
        # going with the current checkout rather than aborting the install.
        git -C "$ANNA_HOME" pull --ff-only || \
            warn "couldn't fast-forward (local edits to tracked files?). Continuing with the current checkout; inspect with: git -C $ANNA_HOME status"
    else
        say "fresh install: cloning $REPO_URL into $ANNA_HOME"
        git clone "$REPO_URL" "$ANNA_HOME"
    fi

    say "creating virtualenv at $ANNA_HOME/.venv"
    "$PYTHON_BIN" -m venv "$ANNA_HOME/.venv"

    # shellcheck disable=SC1091
    source "$ANNA_HOME/.venv/bin/activate"

    say "installing dependencies (this may take a minute)"
    pip install --upgrade pip >/dev/null
    pip install -e "$ANNA_HOME"

    # Seed the markdown vault before the wizard so the operator's first
    # boot of ANNA finds Conversations/, Identity/, agents/, skills/ etc.
    # already in place. The script is idempotent: re-running install.sh
    # never overwrites operator content.
    if [ -x "$ANNA_HOME/scripts/seed_vault.sh" ]; then
        say "seeding markdown vault"
        ANNA_HOME="$ANNA_HOME" bash "$ANNA_HOME/scripts/seed_vault.sh" || \
            warn "vault seed step failed; continuing — re-run scripts/seed_vault.sh manually if needed"
    fi

    say "handing off to the setup wizard"
    exec anna-setup
}

main "$@"
