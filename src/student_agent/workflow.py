from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ACTORS = {
    "order": "order-item-agent",
    "item": "order-item-agent",
    "payment": "payment-agent",
    "refund": "payment-agent",
    "shipment": "shipment-agent",
    "seller": "order-item-agent",
    "policy": "policy-agent",
    "customer": "coordinator",
    "product": "order-item-agent",
}
DOMAINS = tuple(ACTORS)


@dataclass(frozen=True)
class Evidence:
    tool: str
    domain: str
    ref: str
    data: Any


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield _norm(str(key)), child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _dicts(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _dicts(child)


def _values(value: Any, *names: str) -> list[Any]:
    wanted = {_norm(name) for name in names}
    return [child for key, child in _walk(value) if key in wanted]


def _strings(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, (list, tuple, set)):
        return [text for item in value for text in _strings(item)]
    return []


def _first_text(value: Any, *names: str) -> str | None:
    for candidate in _values(value, *names):
        texts = _strings(candidate)
        if texts:
            return texts[0]
    return None


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _first_money(value: Any, *names: str) -> Decimal | None:
    for candidate in _values(value, *names):
        number = _decimal(candidate)
        if number is not None:
            return number
    return None


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _truth(value: Any, *names: str) -> bool:
    return any(
        candidate is True
        or (isinstance(candidate, str) and _norm(candidate) in {"true", "yes", "y", "1"})
        for candidate in _values(value, *names)
    )


def _collect_ids(value: Any) -> dict[str, list[str]]:
    aliases = {
        "order_ids": {"order_id", "order_ids", "claimed_order_id"},
        "item_ids": {"item_id", "item_ids", "order_item_id", "order_item_ids"},
        "seller_ids": {"seller_id", "seller_ids"},
        "payment_references": {
            "payment_reference",
            "payment_references",
            "payment_id",
            "payment_ids",
            "transaction_id",
            "transaction_ids",
            "charge_id",
            "charge_ids",
        },
        "shipment_ids": {
            "shipment_id",
            "shipment_ids",
            "tracking_id",
            "tracking_ids",
            "tracking_code",
            "tracking_codes",
        },
    }
    result = {name: [] for name in aliases}
    for key, child in _walk(value):
        for kind, keys in aliases.items():
            if key not in keys:
                continue
            candidates = _strings(child)
            if not candidates and isinstance(child, (int, float)) and not isinstance(child, bool):
                candidates = [str(child)]
            for candidate in candidates:
                if candidate not in result[kind] and len(result[kind]) < 20:
                    result[kind].append(candidate)
    return result


def _merge_ids(*groups: dict[str, list[str]]) -> dict[str, list[str]]:
    result = {key: [] for key in groups[0]}
    for group in groups:
        for key, values in group.items():
            for value in values:
                if value not in result[key] and len(result[key]) < 20:
                    result[key].append(value)
    return result


def _tool_domain(name: str, schema: Mapping[str, Any]) -> str | None:
    normalized = _norm(name)
    for domain in (
        "refund",
        "payment",
        "shipment",
        "seller",
        "policy",
        "product",
        "item",
        "order",
    ):
        if domain in normalized:
            return domain
    properties = schema.get("properties", {})
    domain_schema = properties.get("domain", {}) if isinstance(properties, Mapping) else {}
    enum = domain_schema.get("enum", []) if isinstance(domain_schema, Mapping) else []
    return str(enum[0]) if len(enum) == 1 and enum[0] in DOMAINS else None


def _candidates(
    case: Mapping[str, Any], ids: dict[str, list[str]], issue: str | None
) -> dict[str, Any]:
    result = {
        key: child for key, child in _walk(case) if isinstance(child, (str, int, float, bool))
    }
    aliases = {
        "order_id": "order_ids",
        "item_id": "item_ids",
        "seller_id": "seller_ids",
        "payment_reference": "payment_references",
        "payment_id": "payment_references",
        "transaction_id": "payment_references",
        "shipment_id": "shipment_ids",
        "tracking_id": "shipment_ids",
        "tracking_code": "shipment_ids",
    }
    for name, kind in aliases.items():
        if ids[kind]:
            result[name] = ids[kind][0]
    if issue:
        result.update({"issue": issue, "issue_code": issue, "topic": issue, "policy_type": issue})
    return result


def _tool_args(
    domain: str, schema: Mapping[str, Any], candidates: Mapping[str, Any]
) -> dict[str, Any] | None:
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    required = schema.get("required", []) if isinstance(schema, Mapping) else []
    if not isinstance(properties, Mapping) or not properties:
        fallback = {
            "seller": ("seller_id",),
            "payment": ("order_id", "payment_reference"),
            "refund": ("order_id", "payment_reference"),
            "shipment": ("order_id", "shipment_id"),
            "item": ("order_id", "item_id"),
            "order": ("order_id",),
            "policy": ("issue_code",),
        }
        for name in fallback.get(domain, ("order_id",)):
            if name in candidates:
                return {name: candidates[name]}
        return {} if domain == "policy" else None
    arguments: dict[str, Any] = {}
    for raw_name, spec in properties.items():
        name = _norm(str(raw_name))
        if name == "case_id":
            continue
        if name in candidates:
            arguments[str(raw_name)] = candidates[name]
        elif isinstance(spec, Mapping) and name == "domain" and domain in spec.get("enum", []):
            arguments[str(raw_name)] = domain
        elif isinstance(spec, Mapping) and len(spec.get("enum", [])) == 1:
            arguments[str(raw_name)] = spec["enum"][0]
    unresolved = (
        {_norm(str(name)) for name in required} - {"case_id"} - {_norm(name) for name in arguments}
    )
    return None if unresolved else arguments


async def _discover(gateway: EvidenceGateway) -> dict[str, dict[str, Any]]:
    describe = getattr(gateway, "describe_tools", None)
    if callable(describe):
        result = await describe()
        if isinstance(result, Mapping):
            return {
                str(name): schema if isinstance(schema, dict) else {}
                for name, schema in result.items()
            }
    return {name: {} for name in await gateway.list_tools()}


async def _consume(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    case_id: str,
    actor: str,
    tool: str,
    arguments: dict[str, Any],
) -> Evidence | None:
    for attempt in range(2):
        try:
            payload = await gateway.call(tool, case_id=case_id, **arguments)
            result = Evidence(
                tool, str(payload["domain"]), str(payload["evidence_ref"]), payload["data"]
            )
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool,
                evidence_refs=[result.ref],
                attributes={"domain": result.domain},
            )
            return result
        except (TimeoutError, ConnectionError):
            if attempt:
                return None
        except (KeyError, TypeError, ValueError, RuntimeError):
            return None
    return None


def _domain_data(evidence: list[Evidence], *domains: str) -> list[Any]:
    return [record.data for record in evidence if record.domain in set(domains)]


def _all_text(values: Iterable[Any]) -> str:
    return " ".join(
        _norm(child) for value in values for _, child in _walk(value) if isinstance(child, str)
    )


def _order_dates(evidence: list[Evidence]) -> tuple[datetime | None, datetime | None]:
    purchase = approved = None
    for data in _domain_data(evidence, "order"):
        purchase = purchase or _date(
            _first_text(data, "order_purchase_timestamp", "purchased_at", "created_at")
        )
        approved = approved or _date(
            _first_text(data, "order_approved_at", "approved_at", "paid_at")
        )
    return purchase, approved


def _event_rows(evidence: list[Evidence], domain: str) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for data in _domain_data(evidence, domain):
        for row in _dicts(data):
            keys = {_norm(str(key)) for key in row}
            if "event_type" in keys and row not in rows:
                rows.append(row)
    return rows


def _near(date: datetime | None, anchor: datetime | None, days: int = 7) -> bool:
    if date is None or anchor is None:
        return anchor is None
    return abs((date - anchor).total_seconds()) <= days * 86400


def _valid_payment_events(evidence: list[Evidence]) -> list[Mapping[str, Any]]:
    purchase, approved = _order_dates(evidence)
    anchor = approved or purchase
    return [
        row
        for row in _event_rows(evidence, "payment")
        if _near(_date(str(row.get("event_at", ""))), anchor)
    ]


def _valid_refund_events(evidence: list[Evidence]) -> list[Mapping[str, Any]]:
    purchase, _ = _order_dates(evidence)
    result = []
    for row in _event_rows(evidence, "refund"):
        event_at = _date(str(row.get("event_at", "")))
        if purchase is None or (event_at is not None and event_at >= purchase):
            result.append(row)
    return result


def _expected_item_total(evidence: list[Evidence]) -> Decimal | None:
    purchase, _ = _order_dates(evidence)
    candidates: dict[str, tuple[float, Decimal]] = {}
    for data in _domain_data(evidence, "item"):
        for index, row in enumerate(_dicts(data)):
            price = _decimal(row.get("price"))
            freight = _decimal(row.get("freight_value")) or Decimal()
            if price is None:
                continue
            item_id = str(row.get("order_item_id") or row.get("item_id") or index)
            limit = _date(str(row.get("shipping_limit_date", "")))
            distance = (
                abs((limit - purchase).total_seconds()) if limit and purchase else float(index)
            )
            current = candidates.get(item_id)
            if current is None or distance < current[0]:
                candidates[item_id] = (distance, price + freight)
    return sum((entry[1] for entry in candidates.values()), Decimal()) if candidates else None


def _totals(evidence: list[Evidence]) -> tuple[Decimal | None, Decimal | None, Decimal]:
    expected = next(
        (
            amount
            for data in _domain_data(evidence, "order", "item")
            if (
                amount := _first_money(
                    data,
                    "order_total_brl",
                    "order_total",
                    "total_order_value",
                    "grand_total",
                    "total_amount",
                    "total_value",
                    "payable_total_brl",
                )
            )
            is not None
        ),
        None,
    )
    expected = expected or _expected_item_total(evidence)
    captured_amounts = [
        amount
        for row in _valid_payment_events(evidence)
        if _norm(str(row.get("event_type", ""))) in {"captured", "capture", "paid"}
        and (amount := _decimal(row.get("amount_brl") or row.get("amount"))) is not None
    ]
    captured = sum(captured_amounts, Decimal()) if captured_amounts else None
    refunded_amounts = [
        amount
        for row in _valid_refund_events(evidence)
        if _norm(str(row.get("status", "")))
        in {"refunded", "completed", "succeeded", "processed", "success"}
        and (amount := _decimal(row.get("amount_brl") or row.get("amount"))) is not None
    ]
    refunded = sum(refunded_amounts, Decimal())
    return expected, captured, refunded


def _payment_count(evidence: list[Evidence]) -> int:
    return sum(
        _norm(str(row.get("event_type", ""))) in {"captured", "capture", "paid"}
        for row in _valid_payment_events(evidence)
    )


def _shipment_issue(evidence: list[Evidence]) -> str | None:
    data = _domain_data(evidence, "shipment", "order")
    actual = next(
        (
            parsed
            for value in data
            if (
                parsed := _date(
                    _first_text(
                        value,
                        "delivered_customer_at",
                        "delivered_at",
                        "actual_delivery_at",
                        "order_delivered_customer_date",
                    )
                )
            )
            is not None
        ),
        None,
    )
    for row in _event_rows(evidence, "shipment"):
        if _norm(str(row.get("event_type", ""))) not in {"delivered_late", "late_delivery"}:
            continue
        event_at = _date(str(row.get("event_at", "")))
        if actual is not None and event_at != actual:
            continue
        actor = _norm(str(row.get("actor", row.get("responsible_party", ""))))
        if actor == "seller":
            return "late_delivery_seller"
        if actor in {"logistics", "logistics_provider", "carrier"}:
            return "late_delivery_logistics"
    text = _all_text(data)
    if any(token in text for token in ("seller_delay", "seller_late", "late_seller")):
        return "late_delivery_seller"
    if any(token in text for token in ("logistics_delay", "carrier_delay", "late_logistics")):
        return "late_delivery_logistics"
    promised = handoff = due = None
    for value in data:
        actual = actual or _date(
            _first_text(
                value, "delivered_at", "actual_delivery_at", "order_delivered_customer_date"
            )
        )
        promised = promised or _date(
            _first_text(
                value,
                "estimated_delivery_at",
                "promised_delivery_at",
                "order_estimated_delivery_date",
            )
        )
        handoff = handoff or _date(
            _first_text(value, "handed_to_carrier_at", "shipped_at", "order_delivered_carrier_date")
        )
        due = due or _date(
            _first_text(value, "expected_handoff_at", "shipping_deadline", "shipping_limit_date")
        )
    if actual and promised and actual > promised:
        return (
            "late_delivery_seller"
            if handoff and due and handoff > due
            else "late_delivery_logistics"
        )
    return None


def _classify(
    evidence: list[Evidence],
) -> tuple[str, float, Decimal | None, Decimal | None, Decimal]:
    expected, captured, refunded = _totals(evidence)
    order_text = _all_text(_domain_data(evidence, "order", "item"))
    payment_text = _all_text(_domain_data(evidence, "payment", "refund"))
    all_data = [record.data for record in evidence]
    paid = captured is not None and captured > refunded
    refund_statuses = {_norm(str(row.get("status", ""))) for row in _valid_refund_events(evidence)}
    duplicate = (
        _truth(all_data, "is_duplicate", "duplicate_charge", "duplicate_capture")
        or any(
            token in payment_text
            for token in ("duplicate_charge", "duplicate_capture", "duplicated_charge")
        )
        or (
            expected is not None
            and captured is not None
            and _payment_count(evidence) > 1
            and captured > expected + Decimal("0.01")
        )
    )
    reconciliation_mismatch = any(
        _norm(str(row.get("event_type", ""))) == "reconciliation_mismatch"
        for row in _valid_payment_events(evidence)
    )
    balanced_split = (
        _payment_count(evidence) > 1
        and expected is not None
        and captured is not None
        and abs(expected - captured) <= Decimal("0.01")
    )
    if (
        any(
            token in order_text
            for token in ("canceled", "cancelled", "order_cancelled", "order_canceled")
        )
        and paid
    ):
        issue, confidence = "canceled_order_paid", 0.95
    elif any(token in order_text for token in ("unavailable", "out_of_stock", "stockout")) and paid:
        issue, confidence = "unavailable_order_paid", 0.95
    elif shipment := _shipment_issue(evidence):
        issue, confidence = shipment, 0.95
    elif reconciliation_mismatch:
        issue, confidence = "payment_mismatch", 0.94
    elif balanced_split:
        issue, confidence = "valid_split_payment", 0.92
    elif "failed" in refund_statuses or any(
        token in payment_text for token in ("refund_failed", "failed_refund")
    ):
        issue, confidence = "refund_failed", 0.95
    elif "pending" in refund_statuses or any(
        token in payment_text for token in ("refund_pending", "pending_refund", "refund_processing")
    ):
        issue, confidence = "refund_pending", 0.93
    elif duplicate:
        issue, confidence = "duplicate_charge", 0.96
    elif (
        expected is not None and captured is not None and abs(expected - captured) > Decimal("0.01")
    ):
        issue, confidence = "payment_mismatch", 0.82
    elif evidence:
        issue, confidence = "unsupported_claim", 0.78
    else:
        issue, confidence = "insufficient_evidence", 0.2
    return issue, confidence, expected, captured, refunded


def _issue_domains(issue: str) -> set[str]:
    if issue.startswith("late_delivery"):
        return {"order", "item", "shipment", "seller", "policy"}
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        return {"order", "item", "product", "payment", "refund", "policy"}
    if issue in {"valid_split_payment", "payment_mismatch", "duplicate_charge"}:
        return {"order", "payment", "policy"}
    if issue.startswith("refund_"):
        return {"payment", "refund", "policy"}
    return set(DOMAINS)


def _policy_rule(evidence: list[Evidence], issue: str) -> Mapping[str, Any] | None:
    for data in _domain_data(evidence, "policy"):
        rules = data.get("rules") if isinstance(data, Mapping) else None
        if isinstance(rules, Mapping) and isinstance(rules.get(issue), Mapping):
            return rules[issue]
    return None


def _claims(
    case: Mapping[str, Any], issue: str, evidence: list[Evidence], confidence: float
) -> list[dict[str, Any]]:
    raw = next(
        (
            candidate
            for candidate in _values(case, "claims", "customer_claims")
            if isinstance(candidate, list)
        ),
        None,
    )
    if raw is None:
        return []
    refs = [record.ref for record in evidence if record.domain in _issue_domains(issue)][:30]
    result = []
    for index, claim in enumerate(raw[:5], 1):
        claim_id = (
            str(claim.get("claim_id") or claim.get("id") or f"claim_{index}")[:64]
            if isinstance(claim, Mapping)
            else f"claim_{index}"
        )
        topic = _norm(str(claim.get("topic", ""))) if isinstance(claim, Mapping) else ""
        if issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif topic == issue:
            verdict = "unsupported" if issue == "unsupported_claim" else "supported"
        elif topic == "requested_full_refund":
            verdict = (
                "supported"
                if issue in {"canceled_order_paid", "unavailable_order_paid"}
                else "unsupported"
            )
        else:
            verdict = "unsupported"
        result.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": refs,
            }
        )
    return result


def _conflicts(evidence: list[Evidence]) -> list[dict[str, Any]]:
    statuses: dict[str, set[str]] = {}
    for record in evidence:
        for value in _values(
            record.data, "order_status", "payment_status", "refund_status", "shipment_status"
        ):
            statuses.setdefault(record.domain, set()).update(
                _norm(item) for item in _strings(value)
            )
    result = []
    for domain, values in statuses.items():
        if (
            len(values & {"canceled", "cancelled", "delivered", "completed", "failed", "refunded"})
            > 1
        ):
            result.append(
                {
                    "field": f"{domain}_status",
                    "sources": sorted(values)[:5],
                    "selected_source": None,
                    "resolution_code": "AUTHORITATIVE_SOURCES_CONFLICT",
                }
            )
    return result[:5]


def _resolution(
    issue: str,
    evidence: list[Evidence],
    ids: dict[str, list[str]],
    expected: Decimal | None,
    captured: Decimal | None,
    refunded: Decimal,
) -> tuple[str, Decimal, list[dict[str, Any]], list[str]]:
    action_issues = {
        "canceled_order_paid",
        "unavailable_order_paid",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "late_delivery_seller",
        "late_delivery_logistics",
    }
    entity = (ids["payment_references"] or ids["order_ids"] or [None])[0]
    amount, reason = Decimal(), None
    rule = _policy_rule(evidence, issue)
    policy_amount = _decimal(rule.get("refund_brl")) if rule else None
    if policy_amount is not None:
        amount = policy_amount
        reason = issue.upper() if amount > 0 else None
    elif (
        issue
        in {"canceled_order_paid", "unavailable_order_paid", "refund_failed", "refund_pending"}
        and captured
    ):
        amount, reason = max(Decimal(), captured - refunded), "OUTSTANDING_REFUND"
    elif issue == "duplicate_charge" and captured:
        amount, reason = max(Decimal(), captured - (expected or Decimal())), "DUPLICATE_CAPTURE"
    elif issue == "payment_mismatch" and captured and expected and captured > expected:
        amount, reason = captured - expected, "OVERCHARGE"
    lines = (
        []
        if not reason or amount <= 0
        else [{"reason_code": reason, "amount_brl": _money(amount), "entity_id": entity}]
    )
    actions = {
        "canceled_order_paid": ["ISSUE_OUTSTANDING_REFUND"],
        "unavailable_order_paid": ["ISSUE_OUTSTANDING_REFUND"],
        "payment_mismatch": ["RECONCILE_PAYMENT", "REFUND_CONFIRMED_OVERCHARGE"],
        "duplicate_charge": ["REFUND_DUPLICATE_CHARGE"],
        "refund_pending": ["MONITOR_OR_ESCALATE_REFUND"],
        "refund_failed": ["RETRY_OR_ESCALATE_REFUND"],
        "late_delivery_seller": ["REMEDIATE_SELLER_DELAY"],
        "late_delivery_logistics": ["REMEDIATE_LOGISTICS_DELAY"],
        "insufficient_evidence": ["REQUEST_ADDITIONAL_EVIDENCE"],
        "unsupported_claim": ["NO_ACTION"],
        "valid_split_payment": ["NO_ACTION"],
    }[issue]
    status = (
        "action_required"
        if issue in action_issues
        else ("needs_investigation" if issue == "insufficient_evidence" else "no_action")
    )
    if rule:
        policy_status = rule.get("case_status")
        policy_action = rule.get("recommended_action")
        if policy_status in {"action_required", "no_action", "needs_investigation"}:
            status = str(policy_status)
        if isinstance(policy_action, str) and policy_action:
            actions = [policy_action]
    return status, amount, lines, actions


def _root_cause(issue: str, evidence: list[Evidence], ids: dict[str, list[str]]) -> dict[str, Any]:
    party_type, party_id = "unknown", None
    if issue == "late_delivery_seller":
        party_type, party_id = "seller", (ids["seller_ids"] or [None])[0]
    elif issue == "late_delivery_logistics":
        party_type, party_id = "logistics_provider", (ids["shipment_ids"] or [None])[0]
    elif issue in {"payment_mismatch", "duplicate_charge", "refund_failed", "refund_pending"}:
        party_type, party_id = "payment_provider", (ids["payment_references"] or [None])[0]
    elif issue in {"canceled_order_paid", "unavailable_order_paid"}:
        party_type = "platform"
    elif issue in {"valid_split_payment", "unsupported_claim"}:
        party_type = "customer"
    rule = _policy_rule(evidence, issue)
    policy_parties = rule.get("responsible_parties") if rule else None
    if isinstance(policy_parties, list) and policy_parties:
        candidate = policy_parties[0]
        if isinstance(candidate, Mapping):
            party_type = str(candidate.get("party_type", party_type))
            party_id = candidate.get("party_id", party_id)
    if party_type == "seller" and party_id is None and ids["seller_ids"]:
        party_id = ids["seller_ids"][0]
    return {
        "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
        "responsible_parties": [{"party_type": party_type, "party_id": party_id}],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate bounded specialist lookups and produce a contract-safe L3A decision."""
    case_id = str(case["case_id"])
    tools = await _discover(gateway)
    evidence: list[Evidence] = []
    ids = _collect_ids(case)
    candidates = _candidates(case, ids, None)

    for tool, schema in tools.items():
        domain = _tool_domain(tool, schema)
        if domain not in {"order", "item", "product", "payment", "shipment", "refund"}:
            continue
        arguments = _tool_args(domain, schema, candidates)
        if arguments is None:
            continue
        actor = ACTORS[domain]
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code=f"QUERY_{domain.upper()}",
        )
        record = await _consume(gateway, trace, case_id, actor, tool, arguments)
        if record and record.ref not in {item.ref for item in evidence}:
            evidence.append(record)

    ids = _merge_ids(ids, *(_collect_ids(record.data) for record in evidence))
    provisional_issue, _, _, _, _ = _classify(evidence)
    candidates = _candidates(case, ids, provisional_issue)
    for tool, schema in tools.items():
        domain = _tool_domain(tool, schema)
        if domain not in {"seller", "policy"}:
            continue
        arguments = _tool_args(domain, schema, candidates)
        if arguments is None:
            continue
        actor = ACTORS[domain]
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code=f"QUERY_{domain.upper()}",
        )
        record = await _consume(gateway, trace, case_id, actor, tool, arguments)
        if record and record.ref not in {item.ref for item in evidence}:
            evidence.append(record)

    issue, confidence, expected, captured, refunded = _classify(evidence)
    ids = _merge_ids(ids, *(_collect_ids(record.data) for record in evidence))
    relevant = [record for record in evidence if record.domain in _issue_domains(issue)][:30]
    refs = [record.ref for record in relevant]
    status, refund, refund_lines, actions = _resolution(
        issue, relevant, ids, expected, captured, refunded
    )
    for actor in sorted({ACTORS.get(record.domain, "coordinator") for record in relevant}):
        actor_refs = [
            record.ref for record in relevant if ACTORS.get(record.domain, "coordinator") == actor
        ][:20]
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="verifier",
            decision_code="EVIDENCE_READY",
            evidence_refs=actor_refs,
        )

    output: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {"primary_issue": issue, "case_status": status, "confidence": confidence},
        "affected_entities": ids,
        "root_cause_analysis": _root_cause(issue, relevant, ids),
        "evidence_refs": refs,
        "data_conflicts": _conflicts(relevant),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _money(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions,
    }
    if claim_assessments := _claims(case, issue, relevant, confidence):
        output["claim_assessments"] = claim_assessments
    policy_refs = [record.ref for record in relevant if record.domain == "policy"][:20]
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier",
        decision_code=issue.upper(),
        evidence_refs=policy_refs,
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="INVARIANTS_PASSED",
        evidence_refs=refs[:20],
        attributes={"evidence_count": len(refs), "refund_brl": _money(refund)},
    )
    return output
