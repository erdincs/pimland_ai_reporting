#!/usr/bin/env python
"""CLI wrapper around the Excel ingestion pipeline.

    python scripts/load_excel.py --file data/raw/sales.xlsx --table sales
"""

from __future__ import annotations

import argparse
import sys

from app.core.logging import configure_logging, get_logger
from app.ingestion.excel_loader import load_excel


def main() -> int:
    parser = argparse.ArgumentParser(description="Load an Excel file into Postgres.")
    parser.add_argument("--file", required=True, help="Path to the .xlsx file")
    parser.add_argument("--table", required=True, help="Target table name")
    parser.add_argument("--sheet", default="0", help="Sheet name or index (default 0)")
    parser.add_argument(
        "--mode",
        default="replace",
        choices=["replace", "append"],
        help="Full reload (replace) or incremental (append)",
    )
    args = parser.parse_args()

    configure_logging()
    log = get_logger("load_excel")

    sheet = int(args.sheet) if args.sheet.isdigit() else args.sheet
    result = load_excel(
        args.file, table=args.table, sheet_name=sheet, if_exists=args.mode
    )
    log.info(
        "done",
        table=result.table,
        rows=result.rows_loaded,
        columns=result.columns,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
