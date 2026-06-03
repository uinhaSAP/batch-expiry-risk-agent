"""Unit tests for risk_scorer.py."""

import sys
import os
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from risk_scorer import score_batch, score_all_batches, _normalise
from models import BatchRecord, RiskResult


def _make_batch(days_to_expiry=30, unit_value=10.0, bin_class="C", material="MAT001", plant="1000"):
    sled = date.today() + timedelta(days=days_to_expiry)
    return BatchRecord(
        batch_number="B001",
        material=material,
        description="Test",
        plant=plant,
        storage_location="WH01",
        bin="BIN01",
        qty_on_hand=500.0,
        qty_on_open_orders=0.0,
        sled=sled,
        days_to_expiry=days_to_expiry,
        unit_value=unit_value,
        unit_of_measure="EA",
        bin_velocity_class=bin_class,
        temperature_zone="AMBIENT",
        hazmat_flag=False,
    )


def _make_risk(risk_qty=100.0, ibp_stale=False, ibp_demand_per_day=1.0):
    return RiskResult(
        batch_number="B001",
        net_risk_qty=risk_qty,
        projected_consumption=0.0,
        risk_qty=risk_qty,
        ibp_demand_per_day=ibp_demand_per_day,
        ibp_data_stale=ibp_stale,
        ibp_data_age_hours=0.0,
    )


def test_normalise_basic():
    assert _normalise(50.0, 100.0) == 0.5
    assert _normalise(0.0, 100.0) == 0.0
    assert _normalise(100.0, 100.0) == 1.0


def test_normalise_zero_max():
    assert _normalise(50.0, 0.0) == 0.0


def test_normalise_capped_at_one():
    assert _normalise(150.0, 100.0) == 1.0


def test_score_bounds():
    batch = _make_batch(days_to_expiry=5)
    risk = _make_risk(risk_qty=100.0)
    score, _ = score_batch(batch, risk, 100.0, 1000.0)
    assert 1 <= score <= 100


def test_score_high_urgency_higher_than_low():
    b_urgent = _make_batch(days_to_expiry=5)
    b_low = _make_batch(days_to_expiry=55)
    r = _make_risk(risk_qty=100.0)
    score_urgent, _ = score_batch(b_urgent, r, 500.0, 1000.0)
    score_low, _ = score_batch(b_low, r, 500.0, 1000.0)
    assert score_urgent > score_low


def test_score_c_bin_higher_than_a_bin():
    b_c = _make_batch(bin_class="C")
    b_a = _make_batch(bin_class="A")
    r = _make_risk(risk_qty=100.0)
    score_c, _ = score_batch(b_c, r, 500.0, 1000.0, risk_horizon_days=60)
    score_a, _ = score_batch(b_a, r, 500.0, 1000.0, risk_horizon_days=60)
    assert score_c >= score_a


def test_confidence_stale_ibp_gives_low():
    batch = _make_batch()
    risk = _make_risk(ibp_stale=True)
    _, confidence = score_batch(batch, risk, 500.0, 1000.0)
    assert confidence == "Low"


def test_confidence_no_demand_gives_medium():
    batch = _make_batch()
    risk = _make_risk(ibp_demand_per_day=0.0, ibp_stale=False)
    _, confidence = score_batch(batch, risk, 500.0, 1000.0)
    assert confidence == "Medium"


def test_confidence_normal_gives_high():
    batch = _make_batch()
    risk = _make_risk(ibp_demand_per_day=5.0, ibp_stale=False)
    _, confidence = score_batch(batch, risk, 500.0, 1000.0)
    assert confidence == "High"


def test_score_all_suppresses_below_threshold():
    b1 = _make_batch(days_to_expiry=58, unit_value=1.0, bin_class="A")  # likely low score
    b2 = _make_batch(days_to_expiry=2, unit_value=100.0, bin_class="C")  # likely high score
    r1 = _make_risk(risk_qty=1.0)
    r2 = _make_risk(risk_qty=500.0)
    results = score_all_batches(
        [(b1, r1), (b2, r2)],
        {("MAT001", "1000"): 600.0},
        min_score_threshold=50,
    )
    # b2 should be above threshold; b1 may or may not be
    assert all(s >= 50 for _, _, s, _ in results)


def test_score_all_sorted_descending():
    batches = [
        (_make_batch(days_to_expiry=5, bin_class="C"), _make_risk(risk_qty=500.0)),
        (_make_batch(days_to_expiry=50, bin_class="A"), _make_risk(risk_qty=10.0)),
    ]
    results = score_all_batches(batches, {("MAT001", "1000"): 510.0}, min_score_threshold=0)
    if len(results) >= 2:
        assert results[0][2] >= results[1][2]


def test_score_all_empty_input():
    results = score_all_batches([], {})
    assert results == []
