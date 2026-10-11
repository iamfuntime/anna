#!/usr/bin/env python3
"""Put back schedule fields an older build erased (rollback -> retrial).

The older build (origin/main 59b946a) drops keys it does not know - ``job_kind``, ``run_safety``,
``overlap_policy``, ``misfire_policy``, ``misfire_grace_seconds`` and anything else added since - every time
it rewrites ``schedules.yaml``. This tool copies such keys back from a backup taken BEFORE the rollback into
the current file. It only ADDS keys that are absent from the current record (never overwrites a value unless
``--overwrite-defaults`` is given and the value is still the default; never touches ``state`` or schedules
missing from either file) and defaults to a dry run. Run it BEFORE the ledger build is started again.

    python scripts/restore_schedule_fields.py CURRENT.yaml BACKUP.yaml            # show what would change
    python scripts/restore_schedule_fields.py CURRENT.yaml BACKUP.yaml --apply    # write (stop the daemon first)

With --apply the current file is first copied to ``CURRENT.yaml.bak-before-restore-<utc>``.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

SKIP = {"state"}
# What the ledger build writes for these keys when it re-saves a record an older build stripped.
DEFAULTS = {"job_kind": "agent_prompt", "run_safety": "side_effecting", "overlap_policy": None,
            "misfire_policy": None, "misfire_grace_seconds": None}


def restorable(current: dict, backup: dict, *, overwrite_defaults: bool = False) -> dict[str, dict]:
    """``{schedule_id: {key: value}}`` of keys the backup has and the current record lacks.

    With ``overwrite_defaults`` a key the current record already carries is also replaced when it still holds
    the default the ledger build wrote after the rollback (use it if the build was already restarted).
    """
    old = {s["id"]: s for s in backup.get("schedules", []) if isinstance(s, dict) and "id" in s}
    out: dict[str, dict] = {}
    for rec in current.get("schedules", []):
        prev = old.get(rec.get("id"))
        if prev is None:
            continue
        missing = {k: v for k, v in prev.items() if k not in SKIP and (
            k not in rec or (overwrite_defaults and k in DEFAULTS and rec[k] == DEFAULTS[k] and v != rec[k]))}
        if missing:
            out[rec["id"]] = missing
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("current", type=Path)
    ap.add_argument("backup", type=Path)
    ap.add_argument("--apply", action="store_true", help="write the merged file (default: dry run)")
    ap.add_argument("--overwrite-defaults", action="store_true",
                    help="also replace ledger-era keys that still hold the default written after the rollback")
    args = ap.parse_args(argv)
    current = yaml.safe_load(args.current.read_text(encoding="utf-8")) or {}
    backup = yaml.safe_load(args.backup.read_text(encoding="utf-8")) or {}
    plan = restorable(current, backup, overwrite_defaults=args.overwrite_defaults)
    if not plan:
        print("nothing to restore")
        return 0
    for sid, fields in plan.items():
        print(f"{sid}: would restore {', '.join(f'{k}={v!r}' for k, v in fields.items())}")
    if not args.apply:
        print("dry run - re-run with --apply (daemon stopped) to write")
        return 0
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(args.current, args.current.with_name(f"{args.current.name}.bak-before-restore-{stamp}"))
    for rec in current["schedules"]:
        rec.update(plan.get(rec.get("id"), {}))
    tmp = args.current.with_suffix(args.current.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(current, sort_keys=False), encoding="utf-8")
    tmp.replace(args.current)
    print(f"restored {sum(len(v) for v in plan.values())} field(s) in {len(plan)} schedule(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
