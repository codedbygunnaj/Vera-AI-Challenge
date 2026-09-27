"""
make_submission.py — builds submission.jsonl (§7.2 of challenge-brief.md):
one composed message per canonical (merchant, trigger[, customer]) test pair,
using the SAME compose() function bot.py serves over HTTP.

Usage:
    python make_submission.py --dataset dataset --out submission.jsonl
"""
import argparse
import json
from pathlib import Path

from composer import compose


def load_json(path: Path):
    with open(path) as f:
        return json.load(f)


def build_index(dataset_dir: Path):
    categories = {}
    for f in (dataset_dir / "categories").glob("*.json"):
        d = load_json(f)
        categories[d["slug"]] = d

    merchants = {}
    for f in (dataset_dir / "merchants").glob("*.json"):
        d = load_json(f)
        merchants[d["merchant_id"]] = d

    customers = {}
    for f in (dataset_dir / "customers").glob("*.json"):
        d = load_json(f)
        customers[d["customer_id"]] = d

    triggers = {}
    for f in (dataset_dir / "triggers").glob("*.json"):
        d = load_json(f)
        triggers[d["id"]] = d

    return categories, merchants, customers, triggers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="dataset")
    ap.add_argument("--out", default="submission.jsonl")
    args = ap.parse_args()

    dataset_dir = Path(args.dataset)
    categories, merchants, customers, triggers = build_index(dataset_dir)
    test_pairs = load_json(dataset_dir / "test_pairs.json")["pairs"]

    lines = []
    for pair in test_pairs:
        test_id = pair["test_id"]
        trigger = triggers[pair["trigger_id"]]
        merchant = merchants[pair["merchant_id"]]
        category = categories[merchant["category_slug"]]
        customer = customers[pair["customer_id"]] if pair.get("customer_id") else None

        composed = compose(category, merchant, trigger, customer)
        lines.append({
            "test_id": test_id,
            "body": composed["body"],
            "cta": composed["cta"],
            "send_as": composed["send_as"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

    with open(args.out, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    print(f"Wrote {len(lines)} lines to {args.out}")


if __name__ == "__main__":
    main()