"""anna-web — stub entry point for the Phase 2.5 web dashboard.

The dashboard hasn't been built yet. This stub exists so the
[project.scripts] entry is present from the cross-platform install plan
onward, which means the real dashboard work doesn't need to touch
pyproject.toml or coordinate a reinstall when it lands.
"""

from __future__ import annotations

import sys


def main() -> int:
    sys.stderr.write(
        "anna-web is part of the Phase 2.5 web dashboard buildout — "
        "see Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md.\n"
        "The dashboard binary will live here once that plan ships; for "
        "now this stub exists only to reserve the entry point.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
