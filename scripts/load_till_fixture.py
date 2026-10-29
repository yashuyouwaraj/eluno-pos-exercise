"""Explicitly import the bundled historical till fixture into a chosen database."""

import argparse
from pathlib import Path

from eluno_pos.operations import OperationsStore


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--confirm-fixture-import",
        action="store_true",
        help="Acknowledge that this imports historical development data into the selected database.",
    )
    args = parser.parse_args()
    if not args.confirm_fixture_import:
        parser.error("pass --confirm-fixture-import to acknowledge the explicit fixture import")
    result = OperationsStore(args.database).load_till_fixture(
        root / "starter-kit" / "data" / "till_sessions_sample.json"
    )
    print(
        "Imported {imported} historical till session(s); skipped {skipped_existing} existing session(s)."
        .format(**result)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
