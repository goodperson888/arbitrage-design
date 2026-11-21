#!/usr/bin/env python3
"""Copy an event file while replacing settlement winners from a reference source."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_reference_winners(path: Path) -> dict[str, str]:
    winners: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            if event.get("eventType") != "SETTLEMENT":
                continue
            winner = event.get("payload", {}).get("winnerOutcome")
            if winner is not None:
                winners[str(event["marketId"])] = str(winner)
    return winners


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile settlement labels from a reference NDJSON file")
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    if args.events.resolve() == args.out.resolve():
        parser.error("--out must differ from --events; raw input is never overwritten")

    reference_winners = load_reference_winners(args.reference)
    compared = 0
    corrected: list[dict[str, str]] = []
    output_events: list[dict[str, object]] = []

    with args.events.open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            if event.get("eventType") == "SETTLEMENT":
                market_id = str(event["marketId"])
                official_winner = reference_winners.get(market_id)
                if official_winner is not None:
                    compared += 1
                    original_winner = str(event.get("payload", {}).get("winnerOutcome"))
                    if original_winner != official_winner:
                        corrected.append({
                            "marketId": market_id,
                            "originalWinner": original_winner,
                            "referenceWinner": official_winner,
                        })
                        event["payload"]["winnerOutcome"] = official_winner
                        source = dict(event.get("source") or {})
                        source["settlementReconciledFrom"] = "Polymarket official Gamma API"
                        event["source"] = source
            output_events.append(event)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for event in output_events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    manifest = {
        "input": str(args.events),
        "reference": str(args.reference),
        "output": str(args.out),
        "referenceSettlementCount": len(reference_winners),
        "comparedSettlementCount": compared,
        "correctedSettlementCount": len(corrected),
        "corrections": corrected,
    }
    manifest_path = args.out.with_suffix(".reconciliation.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Compared {compared} settlements; corrected {len(corrected)}")
    print(f"Output: {args.out}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
