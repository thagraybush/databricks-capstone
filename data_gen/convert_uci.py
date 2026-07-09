"""Convert the UCI Online Retail II xlsx (two sheets) to raw CSVs for volume upload.

Deliberately performs ZERO cleaning — every documented data-quality issue
(22.77% missing Customer ID, C/A-prefix invoices, negative quantities, zero
prices, exact duplicates, the Dec-2010 two-sheet overlap, non-product stock
codes) must arrive intact in bronze. Cleaning is the pipeline's job.

Usage: python data_gen/convert_uci.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

RAW = Path(__file__).parent / "raw"
XLSX = RAW / "online_retail_II.xlsx"
SHEETS = {
    "Year 2009-2010": "online_retail_2009_2010.csv",
    "Year 2010-2011": "online_retail_2010_2011.csv",
}


def main() -> None:
    for sheet, out_name in SHEETS.items():
        df = pd.read_excel(XLSX, sheet_name=sheet, dtype=str)  # dtype=str: no coercion
        out = RAW / out_name
        df.to_csv(out, index=False)
        print(f"{sheet}: {len(df):,} rows → {out.name}")


if __name__ == "__main__":
    main()
