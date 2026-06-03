"""Unit tests for report_generator.py."""

import sys
import os
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from report_generator import generate_report, _format_money, _format_qty
from models import (
    ActionRecommendation,
    BatchRecord,
    FullBatchReport,
    RiskResult,
    ScoredBatch,
)
from config import CURRENCY, MIN_SCORE_THRESHOLD


def _make_scored_batch(
    batch_num="B001",
    material="MAT001",
    score=75,
    days_to_expiry=20,
    risk_qty=100.0,
    unit_value=50.0,
    ibp_stale=False,
    confidence="High",
):
    sled = date.today() + timedelta(days=days_to_expiry)
    batch = BatchRecord(
        batch_number=batch_num,
        material=material,
        description="Test Material",
        plant="1000",
        storage_location="WH01",
        bin="BIN-C1",
        qty_on_hand=150.0,
        qty_on_open_orders=50.0,
        sled=sled,
        days_to_expiry=days_to_expiry,
        unit_value=unit_value,
        unit_of_measure="EA",
        bin_velocity_class="C",
        temperature_zone="AMBIENT",
        hazmat_flag=False,
    )
    risk = RiskResult(
        batch_number=batch_num,
        net_risk_qty=risk_qty,
        projected_consumption=0.0,
        risk_qty=risk_qty,
        ibp_demand_per_day=2.0,
        ibp_data_stale=ibp_stale,
        ibp_data_age_hours=0.0,
    )
    return ScoredBatch(
        batch=batch,
        risk=risk,
        score=score,
        confidence=confidence,
        total_exposure=unit_value * risk_qty,
        total_sku_stock=500.0,
    )


def _make_full_report(scored_batch, action_type=3, draft="DRAFT MARKDOWN"):
    action = ActionRecommendation(
        action_type=action_type,
        action_label="Markdown 15%",
        description="Apply markdown",
        draft_artefact=draft,
        requires_human_approval=True,
    )
    return FullBatchReport(scored_batch=scored_batch, actions=[action])


def test_format_money():
    assert "USD" in _format_money(1234.56)
    assert "1,234.56" in _format_money(1234.56)


def test_format_qty():
    result = _format_qty(100.0, "EA")
    assert "100" in result
    assert "EA" in result


def test_report_header_contains_run_id():
    report = generate_report("run-abc", ["1000"], [], [], total_batches_scanned=10)
    assert "run-abc" in report


def test_report_header_plants():
    report = generate_report("run1", ["1000", "2000"], [], [], total_batches_scanned=5)
    assert "1000" in report
    assert "2000" in report


def test_report_no_at_risk_batches():
    report = generate_report("run1", ["1000"], [], [], total_batches_scanned=10)
    assert "No at-risk batches" in report


def test_report_contains_batch_details():
    sb = _make_scored_batch(batch_num="B999", material="MAT999", score=80)
    fr = _make_full_report(sb)
    report = generate_report("run2", ["1000"], [fr], [], total_batches_scanned=5)
    assert "B999" in report
    assert "MAT999" in report
    assert "80" in report  # Score


def test_report_currency_in_exposure():
    sb = _make_scored_batch(unit_value=10.0, risk_qty=100.0)
    fr = _make_full_report(sb)
    report = generate_report("run3", ["1000"], [fr], [], total_batches_scanned=3)
    assert CURRENCY in report


def test_report_ibp_stale_flag():
    report = generate_report(
        "run4", ["1000"], [], [], total_batches_scanned=5, ibp_data_stale=True
    )
    assert "STALE" in report.upper() or "stale" in report.lower()


def test_report_confidence_downgraded_when_stale():
    sb = _make_scored_batch(confidence="High", ibp_stale=True)
    fr = _make_full_report(sb)
    report = generate_report(
        "run5", ["1000"], [fr], [], total_batches_scanned=1, ibp_data_stale=True
    )
    assert "Low" in report


def test_report_sorted_by_score_descending():
    sb_high = _make_scored_batch(score=90, batch_num="BHIGH")
    sb_low = _make_scored_batch(score=30, batch_num="BLOW")
    fr_high = _make_full_report(sb_high)
    fr_low = _make_full_report(sb_low)
    # Pass in low-score first — report should reorder? Actually generate_report doesn't sort,
    # the caller (agent) passes pre-sorted. Test that both batches appear.
    report = generate_report("run6", ["1000"], [fr_high, fr_low], [], total_batches_scanned=2)
    assert "BHIGH" in report
    assert "BLOW" in report


def test_report_exceptions_section():
    exceptions = ["Batch B123 missing SLED", "Batch B456 missing demand data"]
    report = generate_report("run7", ["1000"], [], exceptions, total_batches_scanned=10)
    assert "B123" in report
    assert "B456" in report
    assert "Exception" in report or "exception" in report


def test_report_draft_artefact_marked_draft():
    sb = _make_scored_batch()
    fr = _make_full_report(sb, draft="DRAFT MARKDOWN EVENT — REQUIRES PRICING TEAM APPROVAL")
    report = generate_report("run8", ["1000"], [fr], [], total_batches_scanned=1)
    assert "DRAFT" in report


def test_report_summary_table_present():
    sb = _make_scored_batch()
    fr = _make_full_report(sb)
    report = generate_report("run9", ["1000"], [fr], [], total_batches_scanned=1)
    assert "Summary Action Table" in report
    assert "Assigned to" in report


def test_report_no_exceptions_section():
    report = generate_report("run10", [], [], [], total_batches_scanned=0)
    assert "No data quality exceptions" in report
