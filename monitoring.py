"""Agrégation des métriques et historique glissant du scanner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


REJECTION_KEYS = (
    "rejected_price", "rejected_old", "rejected_seen", "rejected_pro",
    "rejected_blacklist", "rejected_rule", "rejected_profit", "rejected_score",
)


def build_workflow_report(cycle_results, previous=None, now=None, history_days=30):
    current = now or datetime.now(timezone.utc)
    valid = [row for row in cycle_results if isinstance(row, dict)]
    totals = {}
    opportunities = []
    cycle_seconds = 0.0
    for result in valid:
        for key, value in result.get("stats", {}).items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value
        opportunities.extend(result.get("top_opportunities", []))
        cycle_seconds += float(result.get("duration_seconds", 0) or 0)

    examined = int(totals.get("items_examined", 0))
    rejections = {
        key.removeprefix("rejected_"): {
            "count": int(totals.get(key, 0)),
            "rate_pct": round(totals.get(key, 0) / max(examined, 1) * 100, 2),
        }
        for key in REJECTION_KEYS
    }
    best_by_id = {}
    for row in opportunities:
        item_id = str(row.get("item_id", ""))
        previous_row = best_by_id.get(item_id)
        if previous_row is None or float(row.get("rank_score", 0)) > float(previous_row.get("rank_score", 0)):
            best_by_id[item_id] = row
    top = sorted(
        best_by_id.values(), key=lambda row: float(row.get("rank_score", 0)),
        reverse=True,
    )[:20]

    history = []
    if isinstance(previous, dict):
        history = previous.get("history_30_days", [])
        if not isinstance(history, list):
            history = []
    cutoff = current - timedelta(days=max(1, int(history_days)))
    retained = []
    for entry in history:
        try:
            timestamp = datetime.fromisoformat(str(entry.get("timestamp", "")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if timestamp >= cutoff:
            retained.append(entry)
    catalog_seconds = float(totals.get("catalog_seconds", 0) or 0)
    throughput = examined / max(cycle_seconds, 0.001)
    previous_throughputs = [
        float(entry.get("items_per_second", 0) or 0)
        for entry in retained if float(entry.get("items_per_second", 0) or 0) > 0
    ]
    rolling_throughput = (
        sum(previous_throughputs) / len(previous_throughputs)
        if previous_throughputs else throughput
    )
    regression_pct = (
        max(0.0, (rolling_throughput - throughput) / rolling_throughput * 100)
        if rolling_throughput > 0 else 0.0
    )
    retained.append({
        "timestamp": current.isoformat(timespec="seconds"),
        "items_examined": examined,
        "notifications_sent": int(totals.get("notifications_sent", 0)),
        "candidates": int(totals.get("candidates", 0)),
        "catalog_success": int(totals.get("catalog_success", 0)),
        "catalog_requested": int(totals.get("catalog_requested", 0)),
        "cycle_seconds": round(cycle_seconds, 3),
        "catalog_seconds": round(catalog_seconds, 3),
        "items_per_second": round(throughput, 2),
    })

    return {
        "schema": 2,
        "generated_at": current.isoformat(timespec="seconds"),
        "cycles": len(valid),
        "summary": {
            "listings_received": int(totals.get("catalog_items", 0)),
            "listings_examined": examined,
            "candidates": int(totals.get("candidates", 0)),
            "notifications_sent": int(totals.get("notifications_sent", 0)),
            "searches_completed": int(totals.get("searches_completed", 0)),
            "searches_failed": int(totals.get("searches_failed", 0)),
            "api_requests": int(totals.get("catalog_requested", 0)),
            "api_budget_blocked": int(totals.get("catalog_budget_blocked", 0)),
            "photos_analysed": int(totals.get("photo_analysed", 0)),
            "photos_flagged": int(totals.get("photo_flagged", 0)),
            "photos_failed": int(totals.get("photo_failed", 0)),
        },
        "performance": {
            "cycle_seconds": round(cycle_seconds, 3),
            # Somme des temps réseau de requêtes parallèles. Elle peut être
            # supérieure au temps mur du cycle et ne représente pas un %.
            "catalog_cumulative_seconds": round(catalog_seconds, 3),
            "network_parallelism_ratio": round(
                catalog_seconds / max(cycle_seconds, 0.001), 2,
            ),
            "items_per_second": round(throughput, 2),
            "rolling_30d_items_per_second": round(rolling_throughput, 2),
            "throughput_regression_pct": round(regression_pct, 2),
        },
        "rejection_rates": rejections,
        "best_opportunities": top,
        "history_30_days": retained[-500:],
    }
