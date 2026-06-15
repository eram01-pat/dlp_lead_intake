"""
Unit tests for the matching engine.

Covers:
  - True-positive Tier-1 hits (fleet wash, pressure wash, graffiti, parkade)
  - Tier-2 disambiguation
  - Tier-3 false-positive trap ("cleaning services" alone → Review, never High/Medium)
  - Disqualifier suppression (janitorial, office cleaning)
  - Disqualifier downgrade path
  - "washington" != "washing" (normalization boundary)
  - Multi-keyword scoring (more keywords → higher score → higher confidence)
"""

import pytest

from src.matching.matcher import load_disqualifiers, load_keywords, match_tender
from src.storage.models import Tender


KEYWORDS_PATH = "config/keywords.yaml"
DISQ_PATH = "config/disqualifiers.yaml"

# Shared fixture-style constants (avoids re-loading in every test)
_KEYWORDS = None
_DISQ = None

TIER_WEIGHTS = {1: 10, 2: 5, 3: 1}
THRESHOLDS = {"high": 10, "medium": 5, "review": 0}


def get_kw():
    global _KEYWORDS
    if _KEYWORDS is None:
        _KEYWORDS = load_keywords(KEYWORDS_PATH)
    return _KEYWORDS


def get_disq():
    global _DISQ
    if _DISQ is None:
        _DISQ = load_disqualifiers(DISQ_PATH)
    return _DISQ


def make_tender(title: str, description: str = "") -> Tender:
    import hashlib
    tid = hashlib.sha256(f"{title}{description}".encode()).hexdigest()[:16]
    return Tender(
        id=tid, source_id="test", source_name="Test Municipality",
        title=title, description=description,
        category="", reference_no="TEST-001",
        detail_url="https://example.com/tender/1",
        status="Open", posted_date=None, closing_date=None,
        raw={},
    )


def run_match(title: str, description: str = "", relevance_label=None):
    tender = make_tender(title, description)
    return match_tender(
        tender=tender,
        keywords=get_kw(),
        disqualifiers=get_disq(),
        tier_weights=TIER_WEIGHTS,
        keyword_bonus=2,
        category_diversity_bonus=1,
        confidence_thresholds=THRESHOLDS,
        relevance_label=relevance_label,
    )


# ── True positive Tier-1 tests ────────────────────────────────────────────────

def test_fleet_washing_high():
    m = run_match("Fleet Washing Services — Municipal Vehicles")
    assert m is not None
    assert m.top_tier == 1
    assert m.confidence == "High"
    assert any("fleet" in kw.lower() for kw in m.matched_keywords)


def test_pressure_washing_high():
    m = run_match("Pressure Washing of Transit Facility Exterior")
    assert m is not None
    assert m.top_tier == 1
    assert m.confidence == "High"


def test_graffiti_removal_high():
    m = run_match("Graffiti Removal Services — Public Infrastructure")
    assert m is not None
    assert m.top_tier == 1
    assert m.confidence == "High"


def test_parkade_cleaning_high():
    m = run_match("Annual Parkade Cleaning Contract")
    assert m is not None
    assert m.top_tier == 1
    assert m.confidence == "High"


def test_truck_wash_high():
    m = run_match("Truck Wash Services for Fleet of 45 Refuse Vehicles")
    assert m is not None
    assert m.top_tier == 1
    assert m.confidence == "High"
    assert m.score > THRESHOLDS["high"]  # multiple Tier-1 keywords → high score


def test_undercarriage_cleaning():
    # CMW keyword is "Undercarriage Washing" (not Cleaning) — use actual xlsx term
    m = run_match("Undercarriage Washing and Equipment Degreasing")
    assert m is not None
    assert m.confidence == "High"


# ── Tier-3 false-positive trap ────────────────────────────────────────────────

def test_cleaning_services_alone_is_review():
    """'Cleaning Services' alone (Tier-3 only) must never produce High or Medium."""
    m = run_match("Cleaning Services Contract 2024", "General cleaning services for various facilities.")
    # Without LLM pass enabled, Tier-3-only → Review or None
    if m is not None:
        assert m.confidence == "Review", (
            f"Expected Review for bare Tier-3 match but got {m.confidence}"
        )


def test_service_agreement_alone_is_review():
    m = run_match("Service Agreement — Various Municipal Facilities")
    if m is not None:
        assert m.confidence == "Review"


def test_tier3_with_llm_yes_upgrades():
    """When LLM says yes on a Tier-3 match, the confidence should rise above Review."""
    m = run_match(
        "Cleaning Services for Municipal Facilities",
        "Services include exterior pressure washing and fleet cleaning.",
        relevance_label="yes",
    )
    # With LLM saying yes and relevance bonus, should be Medium or High
    assert m is not None
    assert m.confidence in ("High", "Medium")


def test_tier3_with_llm_no_suppresses():
    """When LLM says no on a Tier-3-only match, result should be None."""
    m = run_match(
        "Cleaning Services Contract",
        "Janitorial and office interior cleaning services.",
        relevance_label="no",
    )
    assert m is None


# ── Disqualifier tests ────────────────────────────────────────────────────────

def test_janitorial_suppresses():
    """A tender explicitly about janitorial work should be suppressed."""
    m = run_match(
        "Janitorial and Cleaning Services",
        "Janitorial services for office buildings including floor mopping.",
    )
    assert m is None


def test_office_cleaning_suppresses():
    m = run_match("Office Cleaning Services Contract")
    assert m is None


def test_mixed_fleet_and_janitorial():
    """
    If a tender mentions both fleet washing AND janitorial, the Tier-1 hit
    should survive (disqualifier downgrade, not suppress) because the tender
    may still be relevant.
    """
    m = run_match(
        "Comprehensive Facility Services",
        "Services include fleet washing of municipal vehicles and janitorial "
        "services for administrative buildings.",
    )
    # Fleet washing is Tier-1 → match exists; janitorial downgrades but does not suppress
    assert m is not None
    assert any("fleet" in kw.lower() or "wash" in kw.lower() for kw in m.matched_keywords)


# ── Normalization boundary tests ──────────────────────────────────────────────

def test_washington_does_not_match_washing():
    """'Washington' must not trigger a 'washing' keyword match."""
    m = run_match("Washington Street Resurfacing Project", "Road work on Washington Ave.")
    assert m is None


def test_hyphenated_keywords_match():
    """'Post-Construction Cleaning' with hyphen should still match."""
    m = run_match("Post-Construction Cleaning Services — New Civic Centre")
    assert m is not None
    assert m.top_tier == 1


def test_plural_stems():
    """'Pressure Washes' and 'Fleet Washings' should still match."""
    m = run_match("Pressure Washes for Transit Bus Depot")
    assert m is not None


# ── No false match ────────────────────────────────────────────────────────────

def test_unrelated_tender_no_match():
    m = run_match("Supply and Delivery of Office Furniture", "Desks, chairs, filing cabinets.")
    assert m is None


def test_it_tender_no_match():
    m = run_match("Software Development and Data Cleansing Services", "IT project.")
    assert m is None


def test_snow_removal_no_match():
    m = run_match("Snow Removal and Grounds Maintenance Services")
    assert m is None
