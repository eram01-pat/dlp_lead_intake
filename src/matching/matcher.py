"""
Tiered keyword matching engine.

Tier 1 — high precision: literal/stemmed hit → High confidence (auto-flag)
Tier 2 — disambiguate:   hit must pass disqualifiers → Medium confidence
Tier 3 — broad net:      hit must pass disqualifiers AND LLM says yes (or goes to Review)

Category signals (from <div id="divCat"> on each tender's detail page):
  - Suppress: bid is in a category that is clearly not CMW scope (Medical, IT, etc.)
  - Boost:    bid is in a category associated with CMW services (Fleet, Public Works, etc.)

Scoring:
  score = tier_weight + keyword_count * keyword_bonus + category_diversity * cat_bonus
          + category_boost
  mapped to confidence: score >= high_threshold → High, >= medium_threshold → Medium, else Review
"""

import logging
import re
from typing import Optional

import yaml

from src.matching.normalize import keyword_pattern, normalize
from src.storage.models import Match, Tender

logger = logging.getLogger(__name__)

# ── Bid category signals ──────────────────────────────────────────────────────
# These are partial-match strings against the category names from divCat.
# Confirm and expand after the first production run reveals the full taxonomy.

# A keyword match on a tender in one of these categories is suppressed entirely
# (regardless of tier) — these are unambiguously not CMW scope.
_CATEGORY_SUPPRESSORS = [
    "medical", "dental", "health service", "health program",
    "pharmaceutical", "nursing", "long term care", "ltc",
    "information technology", "software", "hardware", "it service",
    "telecommunications", "network infrastructure",
    "legal service", "legal counsel", "legal support",
    "financial service", "accounting", "audit",
    "human resource", "staffing", "recruitment",
    "catering", "food service", "cafeteria",
    "insurance",
    "community funding", "grants",
    "library service",
]

# A tender in one of these categories gets a score bonus — these correlate with
# CMW services and reduce false positives for Tier-2/3 keyword hits.
_CATEGORY_BOOSTERS = [
    "fleet", "vehicle", "transit", "bus",
    "public works", "roads", "highway", "transportation",
    "facility maintenance", "building maintenance", "maintenance service",
    "cleaning", "janitorial",          # janitorial is normally a disqualifier in text
    "parking", "garage",               #   but as a category it may contain CMW work
    "environmental service",
    "waste", "sanitation",
    "parks", "recreation facility",
    "fire service", "emergency service",
]

_CATEGORY_BOOST_SCORE = 3


def _check_bid_categories(
    bid_categories: list[str],
) -> tuple[bool, bool]:
    """
    Returns (should_suppress, should_boost).
    Checked against the platform's category taxonomy from divCat.
    """
    cats_lower = " | ".join(bid_categories).lower()

    for term in _CATEGORY_SUPPRESSORS:
        if term in cats_lower:
            return True, False

    for term in _CATEGORY_BOOSTERS:
        if term in cats_lower:
            return False, True

    return False, False


def load_keywords(keywords_path: str) -> list[dict]:
    with open(keywords_path) as f:
        data = yaml.safe_load(f)
    keywords = data.get("keywords", [])
    for kw in keywords:
        kw["_pattern"] = keyword_pattern(kw["keyword"])
    return keywords


def load_disqualifiers(disqualifiers_path: str) -> list[dict]:
    with open(disqualifiers_path) as f:
        data = yaml.safe_load(f)
    disqs = data.get("disqualifiers", [])
    for d in disqs:
        d["_pattern"] = keyword_pattern(d["term"])
    return disqs


def _check_disqualifiers(text: str, disqualifiers: list[dict]) -> tuple[bool, str]:
    norm = normalize(text)
    for d in disqualifiers:
        if d["_pattern"].search(norm):
            return True, d["action"]
    return False, ""


def _confidence_from_score(score: float, thresholds: dict) -> str:
    if score >= thresholds["high"]:
        return "High"
    if score >= thresholds["medium"]:
        return "Medium"
    return "Review"


def _downgrade_confidence(confidence: str) -> str:
    order = ["High", "Medium", "Review"]
    idx = order.index(confidence)
    return order[min(idx + 1, len(order) - 1)]


def match_tender(
    tender: Tender,
    keywords: list[dict],
    disqualifiers: list[dict],
    tier_weights: dict,
    keyword_bonus: float,
    category_diversity_bonus: float,
    confidence_thresholds: dict,
    relevance_label: Optional[str] = None,
    relevance_bonus: float = 0,
) -> Optional[Match]:
    """
    Run the full matching logic for a single tender.
    Returns a Match if any keyword hits; None if no match.
    """
    cats_text = " ".join(tender.bid_categories)
    norm_title = normalize(f"{tender.title} {tender.description}")
    norm_cats  = normalize(cats_text)

    # Category matching is trusted only when the category list is focused
    # (≤8 tags). Tenders with 9+ categories are broad taxonomy dumps where
    # any single tag is unreliable as a scope signal.
    cats_focused = len(tender.bid_categories) <= 8

    matched: list[dict] = []
    for kw in keywords:
        title_hit = kw["_pattern"].search(norm_title)
        cats_hit  = cats_focused and kw["_pattern"].search(norm_cats)
        if title_hit or cats_hit:
            matched.append(kw)

    if not matched:
        return None

    tiers_hit = {kw["tier"] for kw in matched}
    top_tier = min(tiers_hit)

    # ── Bid category signals ──────────────────────────────────────────────────
    cat_suppress, cat_boost = _check_bid_categories(tender.bid_categories)

    if cat_suppress:
        logger.debug(
            "Tender %s suppressed by bid category: %s",
            tender.id, tender.bid_categories,
        )
        return None

    # ── Text disqualifiers ────────────────────────────────────────────────────
    hits_disq, disq_action = _check_disqualifiers(f"{tender.title} {tender.description}", disqualifiers)

    # Tier-1 hits survive text disqualifiers (downgraded, not suppressed)
    if hits_disq and disq_action == "suppress" and top_tier > 1:
        logger.debug("Tender %s suppressed by text disqualifier", tender.id)
        return None
    if hits_disq and disq_action == "suppress" and top_tier == 1:
        disq_action = "downgrade"

    # ── Scoring ───────────────────────────────────────────────────────────────
    tier_weight = tier_weights.get(str(top_tier), tier_weights.get(top_tier, 1))
    kw_categories_hit = list({kw["category"] for kw in matched})
    score = (
        tier_weight
        + len(matched) * keyword_bonus
        + (len(kw_categories_hit) - 1) * category_diversity_bonus
    )

    if cat_boost:
        score += _CATEGORY_BOOST_SCORE

    if relevance_label in ("yes", "maybe") and top_tier >= 2:
        score += relevance_bonus

    confidence = _confidence_from_score(score, confidence_thresholds)

    if hits_disq and disq_action == "downgrade":
        confidence = _downgrade_confidence(confidence)

    # Tier-3-only: keep in Review unless LLM confirms
    if tiers_hit == {3} and relevance_label is None:
        confidence = "Review"
    if tiers_hit == {3} and relevance_label == "no":
        return None

    return Match(
        tender_id=tender.id,
        matched_keywords=[kw["keyword"] for kw in matched],
        categories=kw_categories_hit,
        top_tier=top_tier,
        score=score,
        confidence=confidence,
        relevance_label=relevance_label,
    )
