"""Privacy-safe efficiency summaries for router feedback and matched evaluations."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Iterable


TOKEN_FIELDS = ("input", "cached_input", "output", "reasoning_output", "total")


def _empty_tokens() -> dict[str, int]:
    return {field: 0 for field in TOKEN_FIELDS}


def _add_tokens(target: dict[str, int], usage: dict[str, Any]) -> None:
    for field in TOKEN_FIELDS:
        target[field] += int(usage.get(field, 0) or 0)


def summarize_feedback(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    routes: dict[str, dict[str, Any]] = {}
    labels: dict[str, dict[str, Any]] = {}
    for event in events:
        route_id = str(event.get("routeId", ""))
        if event.get("eventType") == "route_outcome":
            routes[route_id] = event
        elif event.get("eventType") == "human_label":
            labels[route_id] = event

    totals = _empty_tokens()
    labeled_totals = _empty_tokens()
    measured_routes = 0
    measured_labeled = 0
    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "routes": 0,
            "measuredRoutes": 0,
            "labeledRoutes": 0,
            "passedRoutes": 0,
            "partialRoutes": 0,
            "failedRoutes": 0,
            "durationMs": 0,
            "tokens": _empty_tokens(),
        }
    )
    outcomes = {"pass": 0, "partial": 0, "fail": 0}

    for route_id, route in routes.items():
        model = str(route.get("selectedModel", "unknown"))
        model_summary = by_model[model]
        model_summary["routes"] += 1
        model_summary["durationMs"] += int(route.get("durationMs", 0) or 0)
        usage = route.get("observedTokens")
        if isinstance(usage, dict):
            measured_routes += 1
            model_summary["measuredRoutes"] += 1
            _add_tokens(totals, usage)
            _add_tokens(model_summary["tokens"], usage)
        label = labels.get(route_id)
        if not label:
            continue
        outcome = str(label.get("outcome", ""))
        if outcome not in outcomes:
            continue
        outcomes[outcome] += 1
        model_summary["labeledRoutes"] += 1
        outcome_key = {
            "pass": "passedRoutes",
            "partial": "partialRoutes",
            "fail": "failedRoutes",
        }[outcome]
        model_summary[outcome_key] += 1
        if isinstance(usage, dict):
            measured_labeled += 1
            _add_tokens(labeled_totals, usage)

    labeled_count = sum(outcomes.values())
    complete_coverage = labeled_count > 0 and measured_labeled == labeled_count
    tokens_per_pass = None
    if complete_coverage and outcomes["pass"] > 0:
        tokens_per_pass = labeled_totals["total"] / outcomes["pass"]

    model_payload: dict[str, Any] = {}
    for model, values in sorted(by_model.items()):
        routes_count = int(values["routes"])
        labeled_model = int(values["labeledRoutes"])
        passed_model = int(values["passedRoutes"])
        measured_model = int(values["measuredRoutes"])
        model_payload[model] = {
            **values,
            "averageDurationMs": values["durationMs"] / routes_count if routes_count else None,
            "averageObservedTokens": (
                values["tokens"]["total"] / measured_model if measured_model else None
            ),
            "labeledPassRate": passed_model / labeled_model if labeled_model else None,
        }

    return {
        "routeOutcomes": len(routes),
        "tokenMeasuredRoutes": measured_routes,
        "tokenCoverage": measured_routes / len(routes) if routes else 0.0,
        "labeledRoutes": labeled_count,
        "measuredLabeledRoutes": measured_labeled,
        "completeLabeledTokenCoverage": complete_coverage,
        "outcomes": outcomes,
        "observedTokens": totals,
        "observedLabeledTokens": labeled_totals,
        "observedTokensPerPass": tokens_per_pass,
        "byFinalModel": model_payload,
        "billingCostKnown": False,
    }


def summarize_benchmark(records: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cases_by_configuration: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for record in records:
        configuration = str(record["configuration"])
        case_id = str(record["caseId"])
        grouped[configuration].append(record)
        cases_by_configuration[configuration][case_id] = record

    configurations: dict[str, Any] = {}
    for name, items in sorted(grouped.items()):
        accepted = sum(bool(item["accepted"]) for item in items)
        measured = [item for item in items if isinstance(item.get("tokens"), dict)]
        total_tokens = sum(int(item["tokens"].get("total", 0)) for item in measured)
        durations = [int(item.get("durationMs", 0)) for item in items]
        complete = len(measured) == len(items)
        configurations[name] = {
            "cases": len(items),
            "accepted": accepted,
            "acceptanceRate": accepted / len(items) if items else 0.0,
            "tokenMeasuredCases": len(measured),
            "completeTokenCoverage": complete,
            "totalObservedTokens": total_tokens,
            "observedTokensPerAcceptedCase": (
                total_tokens / accepted if complete and accepted else None
            ),
            "medianDurationMs": statistics.median(durations) if durations else None,
            "totalRetries": sum(int(item.get("retries", 0)) for item in items),
        }

    pairwise: list[dict[str, Any]] = []
    names = sorted(cases_by_configuration)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            common = sorted(
                set(cases_by_configuration[left]) & set(cases_by_configuration[right])
            )
            left_only = right_only = both = neither = 0
            token_deltas: list[int] = []
            for case_id in common:
                left_record = cases_by_configuration[left][case_id]
                right_record = cases_by_configuration[right][case_id]
                left_pass = bool(left_record["accepted"])
                right_pass = bool(right_record["accepted"])
                if left_pass and right_pass:
                    both += 1
                    left_tokens = left_record.get("tokens")
                    right_tokens = right_record.get("tokens")
                    if isinstance(left_tokens, dict) and isinstance(right_tokens, dict):
                        token_deltas.append(
                            int(left_tokens.get("total", 0))
                            - int(right_tokens.get("total", 0))
                        )
                elif left_pass:
                    left_only += 1
                elif right_pass:
                    right_only += 1
                else:
                    neither += 1
            pairwise.append({
                "left": left,
                "right": right,
                "matchedCases": len(common),
                "bothAccepted": both,
                "leftOnlyAccepted": left_only,
                "rightOnlyAccepted": right_only,
                "neitherAccepted": neither,
                "meanObservedTokenDeltaOnBothAccepted": (
                    statistics.mean(token_deltas) if token_deltas else None
                ),
            })
    return {"configurations": configurations, "pairwise": pairwise}
