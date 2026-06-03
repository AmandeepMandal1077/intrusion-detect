"""
CLI script to seed the pos_transactions table from a CSV file.

Usage:
    python -m scripts.seed_db --csv data/pos_transactions.csv
    python -m scripts.seed_db --csv data/pos_transactions.csv --store-id store-002
"""

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed pos_transactions table from a CSV file."
    )
    parser.add_argument(
        "--csv",
        required=True,
        help="Path to the CSV file (e.g. data/pos_transactions.csv)",
    )
    parser.add_argument(
        "--store-id",
        default=None,
        help="Override store_id for all rows (useful when CSV lacks the column).",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    from app.database import seed_pos_from_csv

    result = seed_pos_from_csv(str(csv_path), store_id_override=args.store_id)
    print(
        f"Seeding complete: {result['inserted']} inserted, "
        f"{result['skipped']} skipped."
    )


if __name__ == "__main__":
    main()
