"""
End-to-end pagination test for the bids&tenders collector.

Reproduces the Region-of-York miss (2026-07): the listing page's own JS
searches with the platform default page size of 25, so a source with more
open tenders than that silently lost everything past the first page.

Serves a local fake of the platform: a listing page whose inline JS fetches
only the first 25 of 32 tenders (as the real site does), and a search
endpoint that honours start/limit pagination and requires the CSRF token.
The collector must come back with all 32.

The fake search endpoint also requires an opaque session parameter that only
the page's own JS knows, and answers requests without it with an HTML error
page (HTTP 200) — reproducing what the live york site did to a
hand-reconstructed request on 2026-07-13. The collector must therefore
paginate by replaying the page's own captured request, not by building its
own from scratch.

Run directly (no pytest needed):
    python tests/test_bidsandtenders_pagination.py
"""

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MODULE_GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CSRF_TOKEN = "test-csrf-token"
SESSION_PARAM = "opaque-session-value-only-page-js-knows"
TOTAL_TENDERS = 32
DEFAULT_PAGE_SIZE = 25  # what the real listing page's own JS uses

TENDERS = [
    {
        "Id": f"00000000-0000-0000-0000-{i:012d}",
        "Title": f"T26-{i:03d} - Test Tender {i}",
        "Status": "Open",
        "Description": "Only Online Submissions will be Accepted",
        "DateAvailable": "/Date(1751000000000)/",
        "DateClosing": f"/Date({1760000000000 + i * 86_400_000})/",
    }
    for i in range(1, TOTAL_TENDERS + 1)
]

LISTING_HTML = f"""<!doctype html>
<html><head><title>Bid Opportunities</title></head><body>
<input name="__RequestVerificationToken" value="{CSRF_TOKEN}" type="hidden" />
<div id="results"></div>
<script>
  // Mimic the real platform: page JS fires one search with the default page size.
  const params = new URLSearchParams({{
      status: 'Open', limit: '{DEFAULT_PAGE_SIZE}', start: '0',
      dir: 'ASC', from: '', to: '', sort: 'DateClosing ASC,Id',
      session: '{SESSION_PARAM}',
      __RequestVerificationToken: '{CSRF_TOKEN}',
  }});
  fetch('/Module/Tenders/en/Tender/Search/{MODULE_GUID}?' + params.toString(), {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}},
      body: params.toString(),
  }}).then(r => r.json()).then(d => {{
      document.getElementById('results').textContent = d.data.length + ' loaded';
  }});
</script>
</body></html>"""


class _FakePlatform(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if urlparse(self.path).path == "/Module/Tenders/en":
            body = LISTING_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith(f"/Module/Tenders/en/Tender/Search/{MODULE_GUID}"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode())
        query = parse_qs(parsed.query)

        def param(name, default=""):
            return (form.get(name) or query.get(name) or [default])[0]

        if param("__RequestVerificationToken") != CSRF_TOKEN:
            self.send_error(403, "missing CSRF token")
            return

        # Like the live site: a request missing session state gets an HTML
        # error page with HTTP 200, not a JSON error.
        if param("session") != SESSION_PARAM:
            body = b"<html><body>An error occurred processing your request.</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        start = int(param("start", "0"))
        limit = int(param("limit", str(DEFAULT_PAGE_SIZE)))
        payload = json.dumps({
            "success": True,
            "data": TENDERS[start:start + limit],
            "total": TOTAL_TENDERS,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _collect_against_fake(page_limit=None):
    from src.collectors import bidsandtenders

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakePlatform)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    original_limit = bidsandtenders._PAGE_LIMIT
    if page_limit is not None:
        bidsandtenders._PAGE_LIMIT = page_limit

    # Run from a temp cwd so the GUID cache write goes nowhere near the real
    # data/module_endpoints.yaml.
    original_cwd = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            tenders = bidsandtenders.collect(
                source={
                    "id": "testsource",
                    "name": "Test Source",
                    "base_url": f"http://127.0.0.1:{port}",
                },
                user_agent="pagination-test",
                rate_limit_seconds=0,
                timeout_seconds=20,
                fetch_detail_pages=False,
            )
    finally:
        os.chdir(original_cwd)
        bidsandtenders._PAGE_LIMIT = original_limit
        server.shutdown()
    return tenders


def test_collects_all_pages_beyond_default_page_size():
    """32 open tenders, page JS loads 25 — collector must return all 32."""
    tenders = _collect_against_fake()
    assert len(tenders) == TOTAL_TENDERS, f"expected {TOTAL_TENDERS}, got {len(tenders)}"
    assert len({t.id for t in tenders}) == TOTAL_TENDERS, "duplicate tenders after pagination"
    titles = {t.title for t in tenders}
    assert f"T26-{TOTAL_TENDERS:03d} - Test Tender {TOTAL_TENDERS}" in titles, (
        "last-page tender missing — pagination did not run"
    )


def test_paginates_across_multiple_fetches():
    """Force a tiny page limit so the loop must issue several paged requests."""
    tenders = _collect_against_fake(page_limit=10)
    assert len(tenders) == TOTAL_TENDERS, f"expected {TOTAL_TENDERS}, got {len(tenders)}"
    assert len({t.id for t in tenders}) == TOTAL_TENDERS, "duplicate tenders after pagination"


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s — %(message)s")
    test_collects_all_pages_beyond_default_page_size()
    print("PASS: full collection beyond default page size (32/32)")
    test_paginates_across_multiple_fetches()
    print("PASS: multi-page pagination loop (page size 10)")
