"""Offline mail-route evaluation. Reads CSV and writes only IDs and route outcomes."""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from app.contracts import MailReceipt, PolicyClass, Profile, Route
from app.mail.routing import MailRoutingContext, classify_mail, route_mail


_EXPECTED_ACTIONS = {
    "спросить_руководителя": {Route.REVIEW},
    "в_сводку": {Route.RECORD_STATUS},
    "делегировать": {Route.FORWARD_SALES, Route.FORWARD_SERVICE},
    "игнорировать": {Route.IGNORE},
    "авто": {Route.CHECK_CALENDAR},
}
_SENSITIVE_CATEGORIES = {"деньги", "кадры", "обязательства_клиентам", "руководство"}


def evaluate(rows: Iterable[dict[str, str]], context: MailRoutingContext) -> dict[str, object]:
    results = []
    for number, row in enumerate(rows, 1):
        mail = MailReceipt.model_validate({
            "folder": "INBOX",
            "uidvalidity": 1,
            "uid": number,
            "from": row.get("from") or row.get("from_email") or "unknown@example.invalid",
            "subject": row.get("subject", ""),
            "text": row["text"] if "text" in row else row["body"],
            "received_at": datetime(2026, 1, 1, tzinfo=UTC),
        })
        row_context = _benchmark_context(context, row.get("sender_role", ""))
        actual = route_mail(classify_mail(mail, row_context), row_context).route

        label = row.get("expected_action") or row["expected_route"]
        expected = _EXPECTED_ACTIONS.get(label)
        if expected is None:
            expected = {Route(label)}
        sensitivity = row.get("expected_policy_class", "").strip()
        if sensitivity:
            PolicyClass(sensitivity)
        is_sensitive = bool(sensitivity) or row.get("category") in _SENSITIVE_CATEGORIES
        error = None
        if (is_sensitive or expected == {Route.REVIEW}) and actual != Route.REVIEW:
            error = "unsafe_automation" if is_sensitive else "missed_review"
        elif actual not in expected:
            error = "no_decision" if actual == Route.REVIEW else "wrong_route"
        results.append({"id": row["id"], "predicted_route": actual, "expected_action": label, "error": error})
    unresolved = sum(item["error"] == "no_decision" for item in results)
    return {
        "count": len(results),
        "matched": sum(item["error"] is None for item in results),
        "unsafe_automation": sum(item["error"] == "unsafe_automation" for item in results),
        "unresolved_share": unresolved / len(results) if results else 0,
        "results": results,
    }


def _benchmark_context(context: MailRoutingContext, sender_role: str) -> MailRoutingContext:
    # The benchmark provides the role alongside the email. Runtime callers resolve it
    # from trusted configuration and can keep the same route contract.
    if sender_role == "руководитель руководителя":
        return replace(context, sender_policy=PolicyClass.EXECUTIVE_MAIL)
    if sender_role in {"подчинённый", "коллега"}:
        return replace(context, routes={**context.routes, "meeting": Route.CHECK_CALENDAR})
    return context


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate mail routes without reading or sending mail")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--quote-route", choices=[Route.FORWARD_SALES, Route.REVIEW], default=Route.FORWARD_SALES)
    args = parser.parse_args()
    context = MailRoutingContext(
        profile=Profile(profile_id="evaluation", timezone="UTC", quote_route=args.quote_route),
        routes={"access": Route.FORWARD_SERVICE},
        targets={Route.FORWARD_SALES: "sales-desk", Route.FORWARD_SERVICE: "service-desk"},
    )
    with args.input.open(encoding="utf-8-sig", newline="") as source:
        report = evaluate(csv.DictReader(source), context)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
