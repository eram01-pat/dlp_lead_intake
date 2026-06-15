# CMW Tender Monitor

Automated monitor for Canadian Mobile Wash (CMW) that watches 17 Ontario
municipal procurement portals, matches open tenders against CMW's service
keywords, and publishes a daily dashboard of relevant opportunities.

## What it does

1. **Collects** open tenders from 17 bids&tenders.ca portals (all same platform — one collector)
2. **Matches** tenders against a tiered keyword library with disqualifier filtering
3. **Publishes** a static HTML dashboard to GitHub Pages — no server required

## What it does NOT do

- No bidding, no form submission, no document downloads, no login
- Does not cover MERX, City of Ottawa, or City of Toronto (deliberately excluded)
- Only reads publicly available pages while logged out

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/eram01-pat/cmw_lead_intake.git
cd cmw_lead_intake
pip install -r requirements.txt
playwright install chromium
```

### 2. Add the keyword file

Commit `data/CMW_Tender_Keywords.xlsx` (single column of keywords, provided by CMW).
Then generate/merge into `config/keywords.yaml`:

```bash
python scripts/generate_keywords.py
```

Review any `AUTO-ADDED` entries in `config/keywords.yaml` and set the correct `tier`
and `category` for each. See the tier definitions in `config/keywords.yaml`.

### 3. Configure GitHub Secrets

| Secret              | Required | Purpose                                |
|---------------------|----------|----------------------------------------|
| `ANTHROPIC_API_KEY` | Optional | Enables LLM relevance pass for Tier-2/3 |

### 4. Enable GitHub Pages

In repository Settings → Pages → Source: **GitHub Actions**.

### 5. Run manually

```bash
# Full run (collects, matches, builds dashboard)
python pipeline.py

# Single source (for testing/debugging)
python pipeline.py --source vaughan

# Dry run (no DB writes, logs matches to stdout)
python pipeline.py --dry-run
```

---

## Configuration

| File                        | What to edit                                                 |
|-----------------------------|--------------------------------------------------------------|
| `config/sources.yaml`       | Add/remove a municipality (one-line change)                  |
| `config/keywords.yaml`      | Edit tiers, categories, or add keywords                      |
| `config/disqualifiers.yaml` | Add/remove negative terms; confirm with CMW before changing  |
| `config/settings.yaml`      | Rate limits, LLM toggle, confidence thresholds               |

### Adding a municipality

Edit `config/sources.yaml` — add one line:
```yaml
- {id: newcity, name: "City of New City", base_url: "https://newcity.bidsandtenders.ca"}
```
That's all. The collector handles it automatically.

### Tuning the matcher

Keywords are tagged with:
- **tier**: 1 (high-precision, auto-flag), 2 (disambiguate), 3 (broad-net, Review bucket)
- **category**: 1–6 (fleet, parkade, pressure-wash, graffiti, industrial, umbrella)

**If the dashboard is too noisy:** move keywords to a higher tier or add disqualifiers.
**If real opportunities are being missed:** move keywords to a lower tier or add variants.

---

## State persistence

**v1 choice:** `data/tenders.db` (SQLite) is committed back to the repo after each run.

Tradeoff: ~17 noisy commits/week (tagged `[skip ci]`), but zero extra infrastructure and
full history. The schema is designed so a later move to Postgres is a connection-string
change in `src/storage/db.py`.

**Phase-2 alternative:** restore/store the DB as a GitHub Actions artifact or use the
Actions cache — removes the commit noise but loses the history outside of artifacts.

---

## LLM relevance pass (optional)

Set `llm.enabled: true` in `config/settings.yaml` and provide `ANTHROPIC_API_KEY`.

The LLM pass only runs on Tier-2 and Tier-3 candidates (never every tender) to keep
cost bounded. It asks Claude whether exterior/fleet/pressure washing is plausibly in
scope for the tender, and uses the answer to confirm/downgrade ambiguous matches.

With the LLM disabled:
- Tier-1 hits → High confidence (unchanged)
- Tier-2 hits → Medium confidence (pass disqualifiers)
- Tier-3-only hits → Review bucket (never main feed)

---

## Discovery / troubleshooting

See `ACCESS_NOTES.md` for platform discovery notes and the verification checklist
that must be completed before first production run.

If a source breaks (structure change), it logs an error and continues with remaining
sources. The pipeline exits with code 2 if any sources failed (unless `--ignore-errors`
is passed in CI).

---

## Phase-2 upgrade paths (not built in v1)

- **Interactive dashboard**: mark tenders reviewed/dismissed, shared state across
  team — requires a small backend (FastAPI) + persistent DB (Postgres)
- **Email digest**: daily summary of High/Medium matches — thin add-on over same data
- **LLM detail-page enrichment**: fetch tender detail pages for tenders that only had
  a listing-level description
