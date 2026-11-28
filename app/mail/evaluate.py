"""Offline mail-route evaluation. Reads CSV and writes only IDs and route outcomes."""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from app.contracts import MailReceipt, PolicyClass, Profile, Route
from app.mail.routing import MailRoutingContext, classify_mail, route_mail


def evaluate(rows: Iterable[dict[str, str]], context: MailRoutingContext) -> dict[str, object]:
    results = []
    for number, row in enumerate(rows, 1):
        mail = MailReceipt.model_validate({
            "folder": "INBOX", "uidvalidity": 1, "uid": number, "from": row.get("from") or "unknown@example.invalid",
            "subject": row.get("subject", ""), "text": row["text"], "received_at": datetime(2026, 1, 1, tzinfo=UTC),
        })
        actual = route_mail(classify_mail(mail, context), context).route
        expected = Route(row.get("expected_action") or row["expected_route"])
        sensitivity = row.get("expected_policy_class", "").strip()
        if sensitivity:
            PolicyClass(sensitivity)
        error = None
        if (sensitivity or expected == Route.REVIEW) and actual != Route.REVIEW:
            error = "unsafe_automation" if sensitivity else "missed_review"
        elif actual != expected:
            error = "no_decision" if actual == Route.REVIEW else "wrong_route"
        results.append({"id": row["id"], "predicted_route": actual, "expected_route": expected, "error": error})
    unresolved = sum(item["error"] == "no_decision" for item in results)
    return {
        "count": len(results),
        "matched": sum(item["error"] is None for item in results),
        "unsafe_automation": sum(item["error"] == "unsafe_automation" for item in results),
        "unresolved_share": unresolved / len(results) if results else 0,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate mail routes without reading or sending mail")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--quote-route", choices=[Route.FORWARD_SALES, Route.REVIEW], default=Route.FORWARD_SALES)
    args = parser.parse_args()
    context = MailRoutingContext(
        profile=Profile(profile_id="evaluation", timezone="UTC", quote_route=args.quote_route),
        targets={Route.FORWARD_SALES: "sales-desk", Route.FORWARD_SERVICE: "service-desk"},
    )
    with args.input.open(encoding="utf-8-sig", newline="") as source:
        report = evaluate(csv.DictReader(source), context)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
