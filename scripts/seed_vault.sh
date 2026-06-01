#!/usr/bin/env bash
#
# seed_vault.sh: ensure ANNA's markdown vault has its expected directory
# layout and a landing INDEX.md.
#
# Resolution order for the vault root:
#
#   1. $ANNA_VAULT_PATH if set.
#   2. paths.vault_path parsed out of $ANNA_HOME/anna.yaml (or ./anna.yaml
#      if ANNA_HOME is unset), if that file exists and contains a
#      ``vault_path:`` entry under either ``paths:`` or top-level ``vault:``.
#   3. $HOME/Obsidian/ANNA.
#
# The script is fully idempotent:
#
# * mkdir -p never errors on existing dirs.
# * INDEX.md is only written when absent.
# * No existing file in any of the seeded dirs is ever touched.
#
# Invoke from install.sh after the wizard runs, or by the operator at any
# time with ``bash scripts/seed_vault.sh``.
set -euo pipefail

resolve_vault_root() {
    if [ -n "${ANNA_VAULT_PATH:-}" ]; then
        printf '%s\n' "$ANNA_VAULT_PATH"
        return
    fi

    local anna_home="${ANNA_HOME:-$HOME/anna}"
    local yaml_path="$anna_home/anna.yaml"
    if [ ! -f "$yaml_path" ] && [ -f "./anna.yaml" ]; then
        yaml_path="./anna.yaml"
    fi

    if [ -f "$yaml_path" ]; then
        # Look for ``vault_path: <value>`` inside a ``paths:`` block first,
        # then a top-level ``vault:`` block. Grep is good enough for the
        # tightly-controlled shape the wizard writes — we are not parsing
        # arbitrary YAML.
        local extracted
        extracted=$(awk '
            /^paths:/ { in_paths=1; in_vault=0; next }
            /^vault:/ { in_vault=1; in_paths=0; next }
            /^[A-Za-z]/ { in_paths=0; in_vault=0 }
            (in_paths || in_vault) && /^[[:space:]]+(vault_)?path:[[:space:]]*/ {
                sub(/^[[:space:]]+(vault_)?path:[[:space:]]*/, "")
                gsub(/^["'\''[:space:]]+|["'\''[:space:]]+$/, "")
                print
                exit
            }
        ' "$yaml_path" || true)
        if [ -n "$extracted" ]; then
            # Expand ~ if the operator wrote it.
            extracted="${extracted/#\~/$HOME}"
            printf '%s\n' "$extracted"
            return
        fi
    fi

    printf '%s\n' "$HOME/Obsidian/ANNA"
}

main() {
    local vault_root
    vault_root=$(resolve_vault_root)
    # Expand ~ defensively.
    vault_root="${vault_root/#\~/$HOME}"

    mkdir -p "$vault_root"

    local dirs=(
        Conversations
        Daily
        Topics
        Episodic
        Identity
        SubAgents
        agents
        skills
    )
    for d in "${dirs[@]}"; do
        mkdir -p "$vault_root/$d"
    done

    local index="$vault_root/INDEX.md"
    if [ ! -e "$index" ]; then
        cat > "$index" <<'INDEX_EOF'
# ANNA Vault

This is ANNA's markdown vault. Every persistent artifact she writes lands
somewhere under this root. Open it in Obsidian (or any editor) to browse.

## Layout

- `Conversations/` — per-conversation checkpoints. One subdir per
  `transport-dm-or-channel-id`, with timestamped `YYYY-MM-DD-HHMM.md`
  files inside. Written at session close; read on the next resume.
- `Daily/` — daily notes ANNA writes for herself.
- `Topics/` — topical notes she promotes out of conversations.
- `Episodic/` — long-form memories she chose to preserve.
- `Identity/` — archives evicted from her five core identity files.
  Filenames follow `<FILE>-archive-YYYY-MM-DD.md`.
- `SubAgents/` — sub-agent notes and scratch space.
- `agents/` — sub-agent persona files (`<slug>.md`).
- `skills/` — per-agent skill files (`<agent>/<slug>.md`).

ANNA reads and writes here through the Read/Write/Edit/Glob/Grep tools
the conversation worker hands her. Core identity files live OUTSIDE this
vault, under `$ANNA_HOME/core/`.
INDEX_EOF
    fi

    printf '[anna] vault seeded at %s\n' "$vault_root"
}

main "$@"
