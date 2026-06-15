"""
LLM relevance adjudication (Anthropic API).

Called for every new tender in the LLM-first pipeline.
Returns 'yes', 'no', or 'maybe' based on whether Canadian Mobile Wash
could plausibly bid on the work described.
"""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a bid-screening assistant for Canadian Mobile Wash (CMW), a \
commercial mobile washing company operating across southern Ontario. \
CMW dispatches mobile crews and equipment directly to client sites.

CMW'S SERVICES — answer YES or MAYBE if the tender involves any of these:

Fleet & Vehicle Washing:
- Fleet washing — trucks, buses, transit vehicles, municipal fleet, tankers, \
garbage/refuse trucks, trailers, tractor-trailers, semi trucks
- Commercial vehicle washing — all commercial and heavy vehicles
- Heavy equipment washing and degreasing — construction equipment, machinery
- Vehicle sanitization and disinfection — fleet, transit, commercial vehicles
- Undercarriage, chassis, wheel well, aluminum brightening

Parking & Garage Cleaning:
- Underground parking garage cleaning and pressure washing
- Parking lot washing, sweeping, and maintenance
- Parkade / parking structure / parking deck cleaning
- Garage floor cleaning, oil stain removal, floor scrubbing
- Ramp cleaning

Pressure & Power Washing (any commercial surface):
- Pressure washing / power washing / high-pressure cleaning / soft washing
- Exterior building washing — facades, brick, stone, concrete, masonry, \
storefronts, canopies, awnings
- Sidewalk, walkway, and surface washing
- Dumpster pad cleaning, drive-thru cleaning

Graffiti & Surface Restoration:
- Graffiti removal, graffiti abatement — buildings, vehicles, fencing, signage
- Gum removal
- Sign cleaning, fence washing
- Decal removal — lettering, graphics, adhesives from vehicles and surfaces

Warehouse & Industrial Cleaning:
- Warehouse and industrial cleaning — floors, walls, ceilings, racking, beams, \
vents, high dusting
- Manufacturing, distribution centre, plant, factory, production facility cleaning
- Industrial floor scrubbing, degreasing, industrial vacuuming
- Cold storage facility cleaning

Facility & Infrastructure Cleaning:
- Exterior building washing and property maintenance washing
- Transit facility cleaning, bus depot cleaning, fleet terminal cleaning
- Wash bay cleaning, fuel station cleaning
- Dock door washing, loading dock cleaning, shipping/receiving area cleaning
- Garage area washing, garage bin washing
- Bus shelter washing
- Arena cleaning, municipal facility cleaning
- Window cleaning (commercial)
- Stormwater system cleaning, catch basin cleaning

Line Painting (through partnering service — still flag these):
- Parking lot line painting, road marking, pavement marking, re-striping, \
custom stenciling, street sweeping
- Sports field / athletic field marking and line painting (soccer, cricket, \
baseball fields, running tracks)

Post-Construction Cleaning:
- Post-construction cleaning, construction site cleaning services — ONLY when \
explicitly mentioned in the title, description, or bid categories. Do NOT flag \
construction or renovation tenders on the speculation that cleaning might be \
needed afterward.

OUT OF SCOPE — answer NO if the tender is exclusively about:
- Residential cleaning (houses, condos, apartments) — note: CMW does not bid \
residential municipal contracts
- Interior janitorial, office cleaning, or housekeeping services
- Waste collection or garbage removal
- Hazardous waste disposal
- Snow removal or landscaping
- HVAC or duct cleaning
- Carpet cleaning
- Pest control
- Sewer or plumbing services
- Roofing
- Asbestos or mold remediation
- Medical or biohazard cleaning
- Food service or commercial kitchen cleaning
- Construction, renovation, or capital works projects (building/infrastructure \
construction) — NOTE: post-construction cleaning services ARE in scope
- Design, engineering, or consulting services
- Procurement / supply of vehicles, apparatus, or equipment (buying/leasing \
hardware — fire trucks, sweeper machines, fleet vehicles, etc.)
- Cooperative purchasing agreements, standing offers, or vendor-of-record \
arrangements for goods (e.g. Canoe, Sourcewell)

DECISION RULES:
- YES: tender clearly involves one or more CMW services listed above.
- MAYBE: the tender is vague, bundles CMW work with out-of-scope work, or is \
for ongoing operations/maintenance at a facility where CMW services are \
plausible (transit depot, public works yard, operations centre, arena, \
community centre, bus terminal) but the cleaning scope is not explicitly \
stated. When in doubt, answer MAYBE — missing a real opportunity is worse \
than flagging a borderline one.
- NO: tender is exclusively out-of-scope with no plausible CMW angle.\
"""

_USER_TEMPLATE = """\
Tender title: {title}

Description:
{description}

Bid categories: {categories}

Note: if the description is empty or uninformative, base your decision on the \
title and bid categories alone.

Could Canadian Mobile Wash plausibly bid on this tender?
Answer with:
DECISION: yes / no / maybe
REASON: one sentence (max 20 words) explaining why — only include this line if DECISION is yes or maybe\
"""


def adjudicate(
    title: str,
    description: str,
    bid_categories: list[str],
    model: str,
    max_tokens: int,
) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (decision, reason) where decision is 'yes', 'no', 'maybe', or None on error.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("ANTHROPIC_API_KEY not set; skipping LLM pass")
        return None, None

    categories_text = ", ".join(bid_categories) if bid_categories else "None provided"
    description_text = description.strip() if description.strip() else "No description available."

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    for attempt in range(4):
        try:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": _USER_TEMPLATE.format(
                            title=title,
                            description=description_text[:2000],
                            categories=categories_text,
                        ),
                    }
                ],
            )
            raw = message.content[0].text.strip()
            decision, reason = _parse_response(raw, title)
            logger.info("LLM relevance [%s]: %s", decision.upper(), title[:80])
            time.sleep(2.0)
            return decision, reason
        except anthropic.RateLimitError:
            wait = 10 * (2 ** attempt)  # 10s, 20s, 40s, 80s
            logger.warning("Rate limited — waiting %ds before retry (attempt %d/4)", wait, attempt + 1)
            time.sleep(wait)
        except Exception as exc:
            logger.warning("LLM relevance pass failed: %s", exc)
            return None, None

    logger.warning("LLM relevance gave up after 4 rate-limit retries for %r", title)
    return None, None


def _parse_response(raw: str, title: str) -> tuple[str, Optional[str]]:
    """Parse DECISION/REASON lines from LLM response. Falls back gracefully."""
    decision = "maybe"
    reason: Optional[str] = None
    for line in raw.splitlines():
        line = line.strip()
        if line.lower().startswith("decision:"):
            val = line.split(":", 1)[1].strip().lower().rstrip(".")
            if val in ("yes", "no", "maybe"):
                decision = val
            else:
                logger.warning("Unexpected DECISION value %r for %r — treating as maybe", val, title)
        elif line.lower().startswith("reason:"):
            reason = line.split(":", 1)[1].strip()
    if reason is None:
        # Model returned a single word — treat whole response as decision
        single = raw.strip().lower().rstrip(".")
        if single in ("yes", "no", "maybe"):
            decision = single
    return decision, reason
