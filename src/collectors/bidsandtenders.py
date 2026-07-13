"""
Parameterized collector for the bids&tenders.ca (eSolutionsGroup) platform.

All 17 municipalities share this one collector — only base_url differs.

The platform requires JavaScript execution before the search AJAX call will
succeed (JS sets additional cookies / prepares state). We use Playwright
(headless Chromium) to load the listing page once, intercept the AJAX
response, and capture the JSON. Subsequent detail-page fetches use requests.
"""

import hashlib
import logging
import re
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.storage.models import Tender

logger = logging.getLogger(__name__)

_LISTING_PATH = "/Module/Tenders/en"
_DETAIL_PATH  = "/Module/Tenders/en/Tender/Detail"
_CACHE_FILE   = "data/module_endpoints.yaml"
_PAGE_LIMIT   = 100

_REF_PREFIX_RE = re.compile(r"^([A-Z]{1,8}\d{2}-\d{2,5}[A-Z]?)\s*[-–]\s*", re.IGNORECASE)
_GUID_RE       = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


# ── Date parsing ──────────────────────────────────────────────────────────────

def _parse_aspnet_date(value: Any) -> Optional[date]:
    if not value:
        return None
    s = str(value)
    m = re.search(r"/Date\((-?\d+)(?:[+-]\d+)?\)/", s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    return None


# ── HTML helpers ──────────────────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str):
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(p.strip() for p in self._parts if p.strip())


def _strip_html(html: str) -> str:
    if not html:
        return ""
    p = _HTMLStripper()
    p.feed(html)
    return p.get_text()


def _extract_module_guid(html: str) -> Optional[str]:
    m = re.search(
        r'/Module/Tenders/en/Tender/Search/(' + _GUID_RE.pattern + r')',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1)
    m = re.search(
        r'["\'](?:/[^"\']*)?/Tender/Search/(' + _GUID_RE.pattern + r')["\']',
        html, re.IGNORECASE,
    )
    return m.group(1) if m else None


# ── GUID cache ────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    p = Path(_CACHE_FILE)
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def _save_cache(cache: dict) -> None:
    Path(_CACHE_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(_CACHE_FILE).write_text(yaml.dump(cache, default_flow_style=False))


# ── Playwright search ─────────────────────────────────────────────────────────

# The listing page's own JS searches with the platform default page size (25),
# so the passively captured response is only the first page. Once the page is
# loaded we replay the exact search request the page itself made (same URL,
# body, content type, cookies, and browser fingerprint) with only start/limit
# rewritten, paging until every reported item is fetched. Replaying the page's
# own request verbatim matters: a hand-reconstructed request was answered with
# an HTML error page by the live site (york, 2026-07-13) even though the
# documented parameter set was used.
_PAGED_FETCH_JS = """
async ({ url, body, contentType }) => {
    const resp = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': contentType,
            'X-Requested-With': 'XMLHttpRequest',
        },
        body: body,
        credentials: 'same-origin',
    });
    const text = await resp.text();
    try {
        return { status: resp.status, json: JSON.parse(text) };
    } catch (e) {
        return { status: resp.status, snippet: text.slice(0, 300) };
    }
}
"""

_MAX_START = 2000  # runaway-pagination backstop


def _with_paging(form_encoded: str, start: int, limit: int) -> str:
    """Rewrite start/limit in a form-encoded parameter string, keeping the rest."""
    from urllib.parse import parse_qsl, urlencode
    pairs = dict(parse_qsl(form_encoded, keep_blank_values=True))
    pairs["start"] = str(start)
    pairs["limit"] = str(limit)
    return urlencode(pairs)


def _build_fallback_search_req(page, guid: str) -> dict:
    """
    Hand-built search request for pages where no search POST was captured
    (per ACCESS_NOTES.md — known to be rejected by at least york, so this is
    only a fallback when there is nothing to replay).
    """
    token = ""
    try:
        el = page.query_selector('input[name="__RequestVerificationToken"]')
        if el:
            token = el.get_attribute("value") or ""
    except Exception:
        pass
    params = [
        ("status", "Open"), ("limit", str(_PAGE_LIMIT)), ("start", "0"),
        ("dir", "ASC"), ("from", ""), ("to", ""), ("sort", "DateClosing ASC,Id"),
    ]
    if token:
        params.append(("__RequestVerificationToken", token))
    from urllib.parse import urlencode
    body = urlencode(params)
    return {
        "url": f"{_LISTING_PATH}/Tender/Search/{guid}?{body}",
        "body": body,
        "content_type": "application/x-www-form-urlencoded; charset=UTF-8",
    }


def _fetch_all_pages_in_browser(page, search_req: dict, source_id: str) -> Optional[list[dict]]:
    """
    Replay the captured search request from inside the loaded page, paging
    through all results. Returns None if even the first request fails (caller
    falls back to the passively captured items).
    """
    base, _, query = search_req["url"].partition("?")
    body_template = search_req.get("body") or ""
    content_type = search_req.get("content_type") or "application/x-www-form-urlencoded; charset=UTF-8"

    items: list[dict] = []
    start = 0
    total: Optional[int] = None
    while True:
        url = base + ("?" + _with_paging(query, start, _PAGE_LIMIT) if query else "")
        body = _with_paging(body_template, start, _PAGE_LIMIT) if body_template else ""
        try:
            result = page.evaluate(
                _PAGED_FETCH_JS,
                {"url": url, "body": body, "contentType": content_type},
            )
        except Exception as exc:
            logger.warning("%s: in-page search fetch failed at start=%d: %s", source_id, start, exc)
            return None if start == 0 else items
        payload = result.get("json") if isinstance(result, dict) else None
        if not isinstance(payload, dict) or int(result.get("status") or 0) >= 400:
            status = result.get("status") if isinstance(result, dict) else "?"
            snippet = (result.get("snippet") or "")[:200] if isinstance(result, dict) else repr(result)[:200]
            logger.warning(
                "%s: in-page search at start=%d returned non-JSON/HTTP %s: %r",
                source_id, start, status, snippet,
            )
            return None if start == 0 else items
        batch = payload.get("data") or []
        items.extend(batch)
        try:
            total = int(payload.get("total"))
        except (TypeError, ValueError):
            pass
        start += _PAGE_LIMIT
        if (
            not batch
            or len(batch) < _PAGE_LIMIT
            or (total is not None and len(items) >= total)
            or start >= _MAX_START
        ):
            break
    return items


def _dedupe_by_id(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        key = str(it.get("Id") or "")
        if key and key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _fetch_via_playwright(
    base_url: str,
    source_id: str,
    user_agent: str,
    timeout_seconds: int,
    max_per_source: int,
) -> tuple[list[dict], str, dict]:
    """
    Load the listing page in headless Chromium, intercept every AJAX search
    response, then re-run the search in-page with pagination so sources with
    more open tenders than the default page size (25) are fully collected.
    Returns (raw_items, module_guid, cookies_dict).
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    all_items: list[dict] = []
    guid_found: list[str] = []
    totals_reported: list[int] = []
    captured_req: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=user_agent)
        page = ctx.new_page()

        def on_response(response):
            url = response.url
            if "/Tender/Search/" in url and response.request.method == "POST":
                if not guid_found:
                    m = _GUID_RE.search(url)
                    if m:
                        guid_found.append(m.group(0))
                try:
                    body = response.json()
                    items = body.get("data") or []
                    try:
                        totals_reported.append(int(body.get("total")))
                    except (TypeError, ValueError):
                        pass
                    # Keep the page's own request as a replay template for
                    # pagination — this one demonstrably works.
                    if not captured_req:
                        try:
                            captured_req.append({
                                "url": url,
                                "body": response.request.post_data or "",
                                "content_type": response.request.headers.get("content-type", ""),
                            })
                        except Exception:
                            pass
                    if items:
                        all_items.extend(items)
                        logger.info(
                            "%s: captured %d items (total reported: %s)",
                            source_id, len(items), body.get("total"),
                        )
                except Exception as exc:
                    logger.debug("%s: could not parse AJAX response: %s", source_id, exc)

        page.on("response", on_response)

        try:
            page.goto(
                f"{base_url}{_LISTING_PATH}",
                timeout=timeout_seconds * 1000,
                wait_until="domcontentloaded",
            )
            page.wait_for_load_state("networkidle", timeout=timeout_seconds * 1000)
        except PWTimeout:
            logger.warning("%s: page load timed out — using whatever was captured", source_id)

        guid = guid_found[0] if guid_found else ""
        if not guid:
            try:
                guid = _extract_module_guid(page.content()) or ""
            except Exception:
                pass

        total_reported = max(totals_reported) if totals_reported else None

        # Paginate whenever we have a request to replay (or at least a GUID),
        # unless the passive capture already provably got everything.
        search_req = captured_req[0] if captured_req else (
            _build_fallback_search_req(page, guid) if guid else None
        )
        if search_req and not (total_reported is not None and len(all_items) >= total_reported):
            page.remove_listener("response", on_response)
            paged = _fetch_all_pages_in_browser(page, search_req, source_id)
            if paged is not None and len(paged) >= len(all_items):
                logger.info(
                    "%s: paginated search returned %d items (passive capture: %d)",
                    source_id, len(paged), len(all_items),
                )
                all_items = paged
            else:
                logger.warning(
                    "%s: paginated search unusable — keeping %d passively captured items",
                    source_id, len(all_items),
                )

        pw_cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        browser.close()

    all_items = _dedupe_by_id(all_items)

    if total_reported is not None and len(all_items) < total_reported:
        logger.warning(
            "%s: collected only %d of %d reported open tenders — some are being missed",
            source_id, len(all_items), total_reported,
        )

    if max_per_source and len(all_items) > max_per_source:
        all_items = all_items[:max_per_source]

    return all_items, guid, pw_cookies


# ── Detail page ───────────────────────────────────────────────────────────────

def _make_session(user_agent: str, cookies: dict) -> requests.Session:
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(total=0, raise_on_status=False)))
    s.mount("http://",  HTTPAdapter(max_retries=Retry(total=0, raise_on_status=False)))
    s.headers.update({
        "User-Agent":      user_agent,
        "Accept-Language": "en-CA,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    })
    for name, value in cookies.items():
        s.cookies.set(name, value)
    return s


def _extract_categories_from_html(html: str) -> list[str]:
    m = re.search(
        r'<div[^>]*\bid="divCat"[^>]*>(.*?)</div>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return []
    div_content = m.group(1)
    seen: set[str] = set()
    categories: list[str] = []
    for hit in re.finditer(
        r"<li[^>]*>(.*?)(?=<ul|</li>)", div_content, re.DOTALL | re.IGNORECASE
    ):
        text = _strip_html(hit.group(1)).strip()
        if text and text not in seen:
            seen.add(text)
            categories.append(text)
    return categories


def _extract_description_from_html(html: str) -> str:
    """
    Extract the tender scope description from the detail page.

    Tries several patterns used by different versions of the eSolutionsGroup
    bids&tenders platform, falling back gracefully.
    """
    raw = ""

    # Pattern 1: named div like categories uses (id="divDesc" or "divDescription")
    for div_id in ("divDesc", "divDescription", "divScope"):
        m = re.search(
            r'<div[^>]*\bid="' + div_id + r'"[^>]*>(.*?)</div>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if m:
            raw = m.group(1)
            break

    # Pattern 2: <td> label/value layout  "<td>Description:</td><td>...</td>"
    if not raw:
        m = re.search(
            r'<td[^>]*>\s*Description\s*:?\s*</td>\s*<td[^>]*>(.*?)</td>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if m:
            raw = m.group(1)

    # Pattern 3: labelled <span> or <p> following a "Description" heading
    if not raw:
        m = re.search(
            r'(?:Description|Scope\s+of\s+Work)\s*:?\s*</[^>]+>\s*<[^>]+>(.*?)</(?:p|span|div|td)>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if m:
            raw = m.group(1)

    if not raw:
        # Emit a one-line debug snippet so we can identify the real structure next run
        snippet = re.sub(r'\s+', ' ', html[:3000])
        logger.debug("Description field not found; HTML head: %s", snippet[:500])
        return ""

    text = _strip_html(raw)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def _fetch_detail_page(
    session: requests.Session,
    detail_url: str,
    timeout: int,
    rate_limit: float,
) -> tuple[list[str], str]:
    """Returns (bid_categories, description_text). Both empty on error."""
    try:
        r = session.get(detail_url, timeout=timeout, headers={"Accept": "text/html,*/*"})
        if r.status_code != 200:
            logger.debug("Detail page %s returned %s", detail_url, r.status_code)
            return [], ""
        time.sleep(rate_limit)
        categories = _extract_categories_from_html(r.text)
        description = _extract_description_from_html(r.text)
        if not description and logger.isEnabledFor(logging.DEBUG):
            # Emit surrounding context for any "description" occurrence so we can
            # identify the correct HTML pattern from CI logs
            for m in re.finditer(r'description', r.text, re.IGNORECASE):
                pos = m.start()
                logger.debug(
                    "DESC-PROBE %s pos=%d: %r",
                    detail_url, pos, r.text[max(0, pos-80):pos+200],
                )
        return categories, description
    except Exception as exc:
        logger.debug("Detail fetch error %s: %s", detail_url, exc)
        return [], ""


# ── Item parsing ──────────────────────────────────────────────────────────────

def _tender_id(source_id: str, platform_id: str) -> str:
    return hashlib.sha256(f"{source_id}:{platform_id}".encode()).hexdigest()[:32]


def _extract_ref_no(title: str) -> str:
    m = _REF_PREFIX_RE.match(title)
    return m.group(1).upper() if m else ""


def _parse_item(
    item: dict,
    source_id: str,
    source_name: str,
    base_url: str,
    session: requests.Session,
    timeout: int,
    rate_limit: float,
    fetch_details: bool,
) -> Optional[Tender]:
    try:
        platform_id = str(item.get("Id") or "").strip()
        if not platform_id:
            return None
        title = str(item.get("Title") or "").strip()
        if not title:
            return None

        ref_no     = _extract_ref_no(title)
        detail_url = f"{base_url}{_DETAIL_PATH}/{platform_id}"
        bid_type   = ref_no.split("-")[0] if ref_no else ""

        bid_categories: list[str] = []
        description = ""
        if fetch_details:
            bid_categories, description = _fetch_detail_page(session, detail_url, timeout, rate_limit)

        return Tender(
            id=_tender_id(source_id, platform_id),
            source_id=source_id,
            source_name=source_name,
            title=title,
            description=description,
            category=bid_type,
            reference_no=ref_no,
            detail_url=detail_url,
            status=str(item.get("Status") or "Open").strip(),
            posted_date=_parse_aspnet_date(item.get("DateAvailable")),
            closing_date=_parse_aspnet_date(item.get("DateClosing")),
            raw=item,
            bid_categories=bid_categories,
        )
    except Exception as exc:
        logger.warning("Failed to parse item %r: %s", item.get("Id"), exc)
        return None


# ── Public entry point ────────────────────────────────────────────────────────

def collect(
    source: dict,
    user_agent: str,
    rate_limit_seconds: float = 3.0,
    max_retries: int = 3,
    backoff_base_seconds: float = 5.0,
    timeout_seconds: int = 30,
    max_per_source: int = 0,
    fetch_detail_pages: bool = True,
) -> list[Tender]:
    """
    Collect open tenders from one bids&tenders.ca municipality.
    Uses Playwright (headless Chromium) to load the listing page so that
    page JavaScript runs and the AJAX search succeeds.
    Returns [] on unrecoverable error so the pipeline continues.
    """
    source_id   = source["id"]
    source_name = source["name"]
    base_url    = source["base_url"].rstrip("/")

    logger.info("Collecting %s", source_name)

    try:
        raw_items, guid, pw_cookies = _fetch_via_playwright(
            base_url=base_url,
            source_id=source_id,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            max_per_source=max_per_source,
        )
    except Exception as exc:
        logger.error("%s: Playwright fetch failed: %s — skipping", source_name, exc)
        return []

    if not raw_items:
        logger.warning("%s: no items captured from AJAX response", source_name)
        return []

    # Cache the GUID if discovered
    if guid:
        cache = _load_cache()
        if source_id not in cache:
            cache[source_id] = guid
            _save_cache(cache)

    session = _make_session(user_agent, pw_cookies)

    tenders: list[Tender] = []
    for item in raw_items:
        t = _parse_item(
            item=item,
            source_id=source_id,
            source_name=source_name,
            base_url=base_url,
            session=session,
            timeout=timeout_seconds,
            rate_limit=rate_limit_seconds,
            fetch_details=fetch_detail_pages,
        )
        if t:
            tenders.append(t)

    logger.info("%s: %d tenders collected", source_name, len(tenders))
    # Log description extraction quality for the first tender (diagnostic)
    if tenders:
        sample = tenders[0]
        logger.info(
            "%s: sample description [%d chars]: %s",
            source_name, len(sample.description),
            (sample.description[:120] + "…") if len(sample.description) > 120 else (sample.description or "(empty)"),
        )
    return tenders
