#!/usr/bin/env python3
"""Validate raw telemetry JSONL against the contract, splitting clean from rejected.

Every row is checked against ``ingest.schemas.TelemetryEvent``. Duplicate
event_ids are additionally tracked across the whole file, which no single-row
schema can see.

    pip install -e .        # puts the ingest package on the import path
    python ingest/validate.py data/telemetry.jsonl

Writes ``{out}/clean.jsonl`` (normalised, contract-shaped rows) and
``{out}/rejected.jsonl``, where each rejected row is wrapped as::

    {"reasons": ["temp_c: Input should be ..."], "row": {...original...}}

Exits 1 if the rejection rate exceeds --max-reject-rate, so a pipeline step can
fail loudly when a feed degrades. The default --out of ``data`` is deliberate:
the repo already gitignores ``data/*.jsonl``, so both outputs stay uncommitted.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ingest.schemas import TelemetryEvent

CLEAN_FILENAME = "clean.jsonl"
REJECTED_FILENAME = "rejected.jsonl"

REASON_MALFORMED_JSON = "malformed_json"
REASON_NOT_AN_OBJECT = "not_a_json_object"
REASON_DUPLICATE = "duplicate_event_id"

#: Width the reason column is truncated to in the printed summary. The full
#: reason is always written to rejected.jsonl.
REASON_DISPLAY_WIDTH = 68


def format_errors(exc: ValidationError) -> list[str]:
    """Flatten a pydantic error into stable, groupable ``field: message`` strings."""
    reasons: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(row)"
        reasons.append(f"{location}: {error['msg']}")
    return reasons


def summarize(total: int, clean: int, reasons: Counter[str]) -> str:
    rejected = total - clean

    def share(count: int) -> str:
        return f"{count / total:6.2%}" if total else "     -"

    lines = [
        "",
        f"  {'rows':<12}{total:>9,}",
        f"  {'clean':<12}{clean:>9,}  {share(clean)}",
        f"  {'rejected':<12}{rejected:>9,}  {share(rejected)}",
    ]
    if reasons:
        lines += ["", "  rejected by reason (a row may trip more than one):"]
        for reason, count in reasons.most_common():
            shown = reason if len(reason) <= REASON_DISPLAY_WIDTH else reason[: REASON_DISPLAY_WIDTH - 3] + "..."
            lines.append(f"    {shown:<{REASON_DISPLAY_WIDTH}} {count:>7,}")
    lines.append("")
    return "\n".join(lines)


def validate_file(source: Path, out_dir: Path) -> tuple[int, int, Counter[str]]:
    """Split ``source`` into clean and rejected files. Returns (total, clean, reasons)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    clean = 0
    reasons: Counter[str] = Counter()
    #: event_id -> line number of the first row that claimed it. Only rows that
    #: pass the schema register an id, so a malformed row cannot evict a good one.
    seen: dict[str, int] = {}

    with (
        source.open("r", encoding="utf-8") as src,
        (out_dir / CLEAN_FILENAME).open("w", encoding="utf-8", newline="\n") as clean_f,
        (out_dir / REJECTED_FILENAME).open("w", encoding="utf-8", newline="\n") as rejected_f,
    ):

        def reject(row: Any, row_reasons: list[str]) -> None:
            reasons.update(row_reasons)
            rejected_f.write(
                json.dumps({"reasons": row_reasons, "row": row}, separators=(",", ":")) + "\n"
            )

        for line in src:
            line = line.strip()
            if not line:
                continue
            total += 1

            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                reject({"_raw_line": line}, [REASON_MALFORMED_JSON])
                continue

            if not isinstance(raw, dict):
                reject({"_raw_line": line}, [REASON_NOT_AN_OBJECT])
                continue

            try:
                event = TelemetryEvent.model_validate(raw)
            except ValidationError as exc:
                reject(raw, format_errors(exc))
                continue

            if event.event_id in seen:
                reject(raw, [REASON_DUPLICATE])
                continue

            seen[event.event_id] = total
            clean += 1
            clean_f.write(json.dumps(event.to_row(), separators=(",", ":")) + "\n")

    return total, clean, reasons


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate telemetry JSONL against the contract.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("source", type=Path, help="raw JSONL to validate")
    parser.add_argument(
        "--out", type=Path, default=Path("data"), help="directory for clean/rejected output"
    )
    parser.add_argument(
        "--max-reject-rate",
        type=float,
        default=0.10,
        help="exit 1 if the rejection rate exceeds this",
    )
    args = parser.parse_args(argv)

    if not 0.0 <= args.max_reject_rate <= 1.0:
        parser.error("--max-reject-rate must be between 0.0 and 1.0")
    if not args.source.is_file():
        parser.error(f"no such file: {args.source}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    total, clean, reasons = validate_file(args.source, args.out)

    print(f"validated {args.source}")
    print(f"  -> {args.out / CLEAN_FILENAME}")
    print(f"  -> {args.out / REJECTED_FILENAME}")
    print(summarize(total, clean, reasons))

    rate = (total - clean) / total if total else 0.0
    if rate > args.max_reject_rate:
        print(
            f"FAIL: rejection rate {rate:.2%} exceeds --max-reject-rate {args.max_reject_rate:.2%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
