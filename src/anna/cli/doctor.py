"""``anna doctor`` entry point (offline, read-only; see :mod:`anna.doctor`)."""

from __future__ import annotations

import argparse
import json
import sys

from anna.doctor import collect, exit_code, render_plain


def main() -> int:
    parser = argparse.ArgumentParser(prog="anna doctor", description="Offline, read-only health report. "
                                     "Exit 0=ok, 1=warn/unverified, 2=failure.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(sys.argv[1:])
    from anna.config import load_config

    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 - doctor must report, not crash
        msg = {"overall": "fail", "offline": True, "checks": [{
            "id": "config", "title": "Config", "status": "fail", "summary": f"config failed to load: {type(exc).__name__}",
            "action": "fix anna.yaml (see `anna-admin` / the dashboard config editor)", "details": {}}]}
        sys.stdout.write((json.dumps(msg, indent=2) if args.json else render_plain({**msg, "generated_at": "n/a"})) + "\n")
        return 2
    report = collect(config)
    sys.stdout.write((json.dumps(report, indent=2) if args.json else render_plain(report)) + "\n")
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
