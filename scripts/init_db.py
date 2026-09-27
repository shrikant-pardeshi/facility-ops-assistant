"""
(Re)build data/facility_ops.db from data/seed/.

    python scripts/init_db.py           # build only if missing / incomplete
    python scripts/init_db.py --reset   # drop and rebuild (removes tickets created in the app)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from facility_ops.data_access.db import DataUnavailableError, build_database, ensure_database  # noqa: E402
from facility_ops.logging_config import setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="drop and rebuild from seed files")
    args = parser.parse_args()
    setup_logging()
    try:
        if args.reset:
            path = build_database(reset=True)
            print(f"Rebuilt {path}")
        else:
            ensure_database()
            print("Database ready.")
    except DataUnavailableError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
