"""Report generator — Step 5 of the expiry risk scan pipeline.

Produces a structured operational report with per-batch blocks,
summary action table, and data quality exceptions.
"""

import logging
from datetime import datetime, timezone

from models import ActionRecommendation, BatchRecord, FullBatchReport, RiskResult
from config import CURRENCY, MIN_SCORE_THRESHOLD, RISK_HORIZON_DAYS

logger = logging.getLogger(__name__)

_CONFIDENCE_LABEL = {
    "High": "Recommended",
    "Medium": "Consider",
    "Low": "Review manually",
}


def _format_money(amount: float) -> str:
    return f"{CURRENCY} {amount:,.2f}"


def _format_qty(qty: float, uom: str) -> str:
    return f"{qty:,.0f} {uom}"


def generate_report(
    run_id: str,
    plants: list[str],
    batch_reports: list[FullBatchReport],
    exceptions: list[str],
    total_batches_scanned: int,
    ibp_data_stale: bool = False,
    risk_horizon_days: int = RISK_HORIZON_DAYS,
) -> str:
    """Generate the full structured expiry risk report.

    Args:
        run_id: Unique run identifier.
        plants: Plant codes in scope.
        batch_reports: Scored batches with recommended actions.
        exceptions: Data quality exception messages.
        total_batches_scanned: Total batches read before filtering.
        ibp_data_stale: True if any IBP data was stale in this run.
        risk_horizon_days: Scan horizon used.

    Returns:
        Formatted markdown report string.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    plants_str = ", ".join(plants) if plants else "ALL"

    # Calculate total financial exposure
    total_exposure = sum(
        br.scored_batch.batch.unit_value * br.scored_batch.risk.risk_qty
        for br in batch_reports
    )

    lines: list[str] = []

    # ─── HEADER ──────────────────────────────────────────────────────────────
    lines.append("# Batch Expiry Risk Management Report")
    lines.append("")

    if ibp_data_stale:
        lines.append("> ⚠️  **IBP DATA STALE WARNING**: IBP forecast data exceeded freshness threshold.")
        lines.append("> All confidence ratings have been downgraded to **Low**. Verify IBP data before acting.")
        lines.append("")

    lines.append(f"| Field | Value |")
    lines.append(f"|---|---|")
    lines.append(f"| Run ID | `{run_id}` |")
    lines.append(f"| Run timestamp | {now} |")
    lines.append(f"| Plant(s) covered | {plants_str} |")
    lines.append(f"| Scan horizon | {risk_horizon_days} days |")
    lines.append(f"| Total batches scanned | {total_batches_scanned} |")
    lines.append(f"| At-risk batches (score ≥ {MIN_SCORE_THRESHOLD}) | {len(batch_reports)} |")
    lines.append(f"| Total financial exposure | {_format_money(total_exposure)} |")
    lines.append("")

    if not batch_reports:
        lines.append("**No at-risk batches found above score threshold for this run.**")
        lines.append("")
    else:
        # ─── PER-BATCH BLOCKS ─────────────────────────────────────────────────
        lines.append("---")
        lines.append("")
        lines.append("## At-Risk Batch Details")
        lines.append("")

        for br in batch_reports:
            b = br.scored_batch.batch
            r = br.scored_batch.risk
            score = br.scored_batch.score
            confidence = br.scored_batch.confidence
            if ibp_data_stale:
                confidence = "Low"
            conf_label = _CONFIDENCE_LABEL.get(confidence, confidence)

            exposure = b.unit_value * r.risk_qty

            lines.append(f"### Batch `{b.batch_number}` — {b.material}: {b.description}")
            lines.append("")
            lines.append(f"| Field | Value |")
            lines.append(f"|---|---|")
            lines.append(f"| Plant / Location / Bin | {b.plant} / {b.storage_location} / {b.bin} |")
            lines.append(f"| Qty at risk | {_format_qty(r.risk_qty, b.unit_of_measure)} |")
            lines.append(f"| Unit value | {_format_money(b.unit_value)} |")
            lines.append(f"| Total exposure | **{_format_money(exposure)}** |")
            lines.append(f"| SLED | {b.sled} |")
            lines.append(f"| Days to expiry | **{b.days_to_expiry}** |")
            lines.append(f"| Risk score | **{score}/100** |")
            lines.append(f"| Confidence | {confidence} — *{conf_label}* |")
            lines.append("")

            lines.append("**Recommended Actions:**")
            lines.append("")
            for action in br.actions:
                lines.append(f"- **[Action {action.action_type}] {action.action_label}**: {action.description}")
            lines.append("")

            # Draft artefacts
            for action in br.actions:
                if action.draft_artefact:
                    lines.append(f"<details>")
                    lines.append(f"<summary>📄 Draft: {action.action_label}</summary>")
                    lines.append("")
                    lines.append("```")
                    lines.append(action.draft_artefact)
                    lines.append("```")
                    lines.append("")
                    lines.append("</details>")
                    lines.append("")

            lines.append("---")
            lines.append("")

        # ─── SUMMARY ACTION TABLE ─────────────────────────────────────────────
        lines.append("## Summary Action Table")
        lines.append("")
        lines.append("| Batch # | Material | Risk Score | Days to Expiry | Recommended Action | Draft Ready? | Assigned to |")
        lines.append("|---|---|---|---|---|---|---|")

        for br in batch_reports:
            b = br.scored_batch.batch
            score = br.scored_batch.score
            top_action = br.actions[0] if br.actions else None
            action_label = top_action.action_label if top_action else "—"
            draft_ready = "✅ Yes" if (top_action and top_action.draft_artefact) else "❌ No"
            lines.append(
                f"| `{b.batch_number}` | {b.material} | {score} | {b.days_to_expiry} | "
                f"{action_label} | {draft_ready} | _(blank)_ |"
            )

        lines.append("")

    # ─── EXCEPTIONS ──────────────────────────────────────────────────────────
    if exceptions:
        lines.append("## Data Quality Exceptions")
        lines.append("")
        lines.append("The following batches were skipped due to data quality issues. Flag for master data correction:")
        lines.append("")
        for exc in exceptions:
            lines.append(f"- {exc}")
        lines.append("")
    else:
        lines.append("## Data Quality Exceptions")
        lines.append("")
        lines.append("_No data quality exceptions in this run._")
        lines.append("")

    return "\n".join(lines)
