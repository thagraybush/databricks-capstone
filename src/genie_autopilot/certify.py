"""Human certification loop for draft benchmark questions.

For each uncertified entry: shows the question, stratum, and note; EXECUTES the
golden SQL against the live warehouse and prints the first rows — so the
certification decision is "does this result actually answer this question?",
not "does this SQL look plausible?". Decisions write back to the YAML
immediately (crash-safe), so you can stop and resume anytime.

Keys:  y = certify   n = reject (kept in file, marked rejected)   s = skip
       e = certify but flag for later edit                        q = quit (saved)

Usage: python -m genie_autopilot.certify [--file benchmarks/retail_questions_draft.yaml]
       [--stratum clean|jargon|collision|bad] [--no-execute]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from . import config

REPO_ROOT = Path(__file__).resolve().parents[2]


class _LiteralStr(str):
    pass


def _literal_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.add_representer(_LiteralStr, _literal_representer)


def _save(doc: dict, path: Path) -> None:
    for q in doc.get("questions", []):
        if q.get("answer_sql") and "\n" in str(q["answer_sql"]):
            q["answer_sql"] = _LiteralStr(q["answer_sql"])
    path.write_text(yaml.dump(doc, sort_keys=False, allow_unicode=True, width=100))


def _run_golden(w, sql: str, warehouse_id: str) -> str:
    try:
        r = w.statement_execution.execute_statement(
            statement=sql, warehouse_id=warehouse_id, wait_timeout="50s"
        )
        state = r.status.state.value
        if state != "SUCCEEDED":
            return f"  ⚠ SQL {state}: {(r.status.error.message or '')[:200]}"
        cols = [c.name for c in (r.manifest.schema.columns or [])] if r.manifest else []
        rows = (r.result.data_array or [])[:5] if r.result else []
        out = ["  → " + " | ".join(cols)]
        out += ["    " + " | ".join(str(v) for v in row) for row in rows]
        if not rows:
            out.append("    (0 rows — is that expected for this question?)")
        return "\n".join(out)
    except Exception as exc:
        return f"  ⚠ execution failed: {str(exc)[:200]}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(REPO_ROOT / "benchmarks" / "retail_questions_draft.yaml"))
    ap.add_argument("--stratum", choices=["clean", "jargon", "collision", "bad"])
    ap.add_argument("--no-execute", action="store_true")
    ap.add_argument("--warehouse", default="b9f4a06641eedd7b")
    args = ap.parse_args()

    path = Path(args.file)
    doc = yaml.safe_load(path.read_text())
    w = None if args.no_execute else config.workspace_client()

    def stratum_of(q):
        t = q.get("trap")
        return "jargon" if t is True else ("bad" if t == "bad" else (t if t == "collision" else "clean"))

    pending = [
        q for q in doc["questions"]
        if not q.get("certified") and not q.get("rejected")
        and (not args.stratum or stratum_of(q) == args.stratum)
    ]
    print(f"{len(pending)} uncertified entries" + (f" in stratum '{args.stratum}'" if args.stratum else ""))

    done = 0
    for q in pending:
        print("\n" + "=" * 78)
        print(f"[{stratum_of(q)}]  {q['q']}")
        if q.get("note"):
            print(f"  note: {q['note']}")
        if q.get("answer_sql"):
            print("  golden SQL:")
            for ln in str(q["answer_sql"]).strip().splitlines():
                print(f"    {ln}")
            if w is not None:
                print(_run_golden(w, str(q["answer_sql"]), args.warehouse))
        else:
            print("  (noise entry — no SQL; certify = 'yes, this is realistically unanswerable')")
        try:
            choice = input("  [y]certify  [n]reject  [e]certify+flag-edit  [s]kip  [q]uit > ").strip().lower()
        except EOFError:
            choice = "q"
        if choice == "y":
            q["certified"] = True
        elif choice == "e":
            q["certified"] = True
            q["needs_edit"] = True
        elif choice == "n":
            q["rejected"] = True
        elif choice == "q":
            break
        else:
            continue
        done += 1
        _save(doc, path)  # crash-safe: persist after every decision

    _save(doc, path)
    certified = sum(1 for q in doc["questions"] if q.get("certified"))
    rejected = sum(1 for q in doc["questions"] if q.get("rejected"))
    print(f"\nsession: {done} decisions · file now: {certified} certified, {rejected} rejected, "
          f"{len(doc['questions']) - certified - rejected} pending")
    if certified:
        print("next: ask the agent to 'sync certified benchmarks and re-baseline' "
              "(loads certified entries to the Genie space and runs the expanded eval).")


if __name__ == "__main__":
    main()
