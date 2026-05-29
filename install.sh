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
    command -v "$cmd" >/dev/null 2>&1 || die "missing prerequisite: $name ($cmd)"
}

check_python_version() {
    local version
    version=$("$PYTHON_BIN" -c 'import sys; print("{}.{}".format(*sys.version_info[:2]))')
    case "$version" in
        3.11|3.12|3.13|3.14|3.15)
            say "python $version detected"
            ;;
        *)
            die "ANNA requires Python 3.11+, found $version"
            ;;
    esac
}

main() {
    say "checking prerequisites"
    check_prereq "git" "git"
    check_prereq "curl" "curl"
    check_prereq "python3" "$PYTHON_BIN"
    check_python_version

    if [ -d "$ANNA_HOME/.git" ]; then
        say "existing checkout at $ANNA_HOME, pulling latest"
        git -C "$ANNA_HOME" pull --ff-only
    else
        say "cloning $REPO_URL into $ANNA_HOME"
        git clone "$REPO_URL" "$ANNA_HOME"
    fi

    say "creating virtualenv at $ANNA_HOME/.venv"
    "$PYTHON_BIN" -m venv "$ANNA_HOME/.venv"

    # shellcheck disable=SC1091
    source "$ANNA_HOME/.venv/bin/activate"

    say "installing dependencies (this may take a minute)"
    pip install --upgrade pip >/dev/null
    pip install -e "$ANNA_HOME"

    say "handing off to the setup wizard"
    exec anna-setup
}

main "$@"
