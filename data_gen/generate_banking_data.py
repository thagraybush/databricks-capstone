"""Generate deterministic synthetic banking data (no real PII, seeded RNG).

Outputs batched INSERT statements to data_gen/output/inserts.sql so loading works
on Free Edition through any SQL surface (warehouse editor, SQL connector, jobs).

Usage: python data_gen/generate_banking_data.py [--customers 200 --transactions 5000]
"""

from __future__ import annotations

import argparse
import random
from datetime import date, timedelta
from pathlib import Path

FIRST = ["Ava", "Liam", "Maya", "Noah", "Zoe", "Ethan", "Iris", "Owen", "Ruth", "Cole"]
LAST = ["Alvarez", "Chen", "Dubois", "Eze", "Fisher", "Gupta", "Haas", "Ito", "Jones", "Klein"]
SEGMENTS = ["Mass Affluent", "High Net Worth", "Retail"]
ACCOUNT_TYPES = ["Checking", "Savings"]
BATCH = 250


def rows_to_insert(table: str, cols: list[str], rows: list[tuple]) -> list[str]:
    stmts = []
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        values = ",\n".join(
            "(" + ", ".join(f"'{v}'" if isinstance(v, str) else str(v) for v in r) + ")"
            for r in chunk
        )
        stmts.append(f"INSERT INTO {table} ({', '.join(cols)}) VALUES\n{values};")
    return stmts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--customers", type=int, default=200)
    ap.add_argument("--transactions", type=int, default=5000)
    ap.add_argument("--schema", default="workspace.banking_gold")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    base = date(2026, 1, 1)

    customers, portfolios, transactions = [], [], []
    for i in range(args.customers):
        cid = f"C{i:05d}"
        seg = rng.choices(SEGMENTS, weights=[3, 1, 6])[0]
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        customers.append((cid, name, seg, str(base + timedelta(days=rng.randint(0, 120)))))
        # Wealth portfolios exist mostly for the non-Retail segments — the cross-BU trap.
        if seg != "Retail" or rng.random() < 0.1:
            portfolios.append(
                (
                    f"P{i:05d}",
                    cid,
                    round(rng.uniform(10_000, 900_000), 2),
                    round(rng.uniform(50_000, 5_000_000), 2),
                    str(base + timedelta(days=rng.randint(120, 180))),
                )
            )

    for t in range(args.transactions):
        cid = f"C{rng.randrange(args.customers):05d}"
        transactions.append(
            (
                f"T{t:07d}",
                cid,
                rng.choice(ACCOUNT_TYPES),
                round(rng.uniform(-60_000, 60_000), 2),
                round(rng.uniform(100, 250_000), 2),
                str(base + timedelta(days=rng.randint(0, 180))),
            )
        )

    out = Path(__file__).parent / "output"
    out.mkdir(exist_ok=True)
    stmts: list[str] = []
    stmts += rows_to_insert(
        f"{args.schema}.dim_customers",
        ["customer_id", "customer_name", "segment", "onboarded_date"],
        customers,
    )
    stmts += rows_to_insert(
        f"{args.schema}.fact_wealth_portfolios",
        ["portfolio_id", "customer_id", "liquid_cash_assets", "invested_market_value", "last_valuation_date"],
        portfolios,
    )
    stmts += rows_to_insert(
        f"{args.schema}.fact_transactions",
        ["transaction_id", "customer_id", "account_type", "amount", "available_balance", "posted_date"],
        transactions,
    )
    (out / "inserts.sql").write_text("\n\n".join(stmts))
    print(
        f"Wrote {len(customers)} customers, {len(portfolios)} portfolios, "
        f"{len(transactions)} transactions → {out / 'inserts.sql'}"
    )


if __name__ == "__main__":
    main()
