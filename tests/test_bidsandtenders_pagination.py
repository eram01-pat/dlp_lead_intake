"""
End-to-end pagination test for the bids&tenders collector.

Reproduces the Region-of-York miss (2026-07): the listing page's own JS
searches with the platform default page size of 25, so a source with more
open tenders than that silently lost everything past the first page.

Serves a local fake of the platform: a listing page whose inline JS fetches
only the first page of 32 tenders (as the real site does), and a search
endpoint that honours start pagination and requires the CSRF token.
The collector must come back with all 32.

The fake search endpoint is deliberately strict, mirroring the live site's
observed behaviour (york/peelregion, 2026-07-13 dry runs):

- requests missing an opaque session parameter that only the page's own JS
  knows are answered with an HTML error page over HTTP 200 (a hand-built
  request must fail);
- requests whose `limit` differs from the page's own are rejected the same
  way (a replay that rewrites the page size must fail).

The collector must therefore paginate by replaying the page's own captured
request byte-for-byte, substituting only the `start` value.

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


def _listing_html(page_size: int) -> str:
    return f"""<!doctype html>
<html><head><title>Bid Opportunities</title></head><body>
<input name="__RequestVerificationToken" value="{CSRF_TOKEN}" type="hidden" />
<div id="results"></div>
<script>
  // Mimic the real platform: page JS fires one search with its page size.
  const params = new URLSearchParams({{
      status: 'Open', limit: '{page_size}', start: '0',
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


def _make_handler(page_size: int):
    class _FakePlatform(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _html_error(self):
            # Like the live site: rejected requests get an HTML error page
            # with HTTP 200, not a JSON error.
            body = b"<html><body><h2>An error has occurred</h2></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urlparse(self.path).path == "/Module/Tenders/en":
                body = _listing_html(page_size).encode()
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
            if param("session") != SESSION_PARAM:
                self._html_error()
                return
            if param("limit") != str(page_size):
                self._html_error()
                return

            start = int(param("start", "0"))
            payload = json.dumps({
                "success": True,
                "data": TENDERS[start:start + page_size],
                "total": TOTAL_TENDERS,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return _FakePlatform


def _collect_against_fake(page_size: int):
    from src.collectors import bidsandtenders

    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(page_size))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

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
        server.shutdown()
    return tenders


def test_collects_all_pages_beyond_default_page_size():
    """32 open tenders, page JS loads 25 — collector must return all 32."""
    tenders = _collect_against_fake(page_size=25)
    assert len(tenders) == TOTAL_TENDERS, f"expected {TOTAL_TENDERS}, got {len(tenders)}"
    assert len({t.id for t in tenders}) == TOTAL_TENDERS, "duplicate tenders after pagination"
    titles = {t.title for t in tenders}
    assert f"T26-{TOTAL_TENDERS:03d} - Test Tender {TOTAL_TENDERS}" in titles, (
        "last-page tender missing — pagination did not run"
    )


def test_paginates_across_multiple_fetches():
    """A small page size forces the loop through several paged requests."""
    tenders = _collect_against_fake(page_size=10)
    assert len(tenders) == TOTAL_TENDERS, f"expected {TOTAL_TENDERS}, got {len(tenders)}"
    assert len({t.id for t in tenders}) == TOTAL_TENDERS, "duplicate tenders after pagination"


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s — %(message)s")
    test_collects_all_pages_beyond_default_page_size()
    print("PASS: full collection beyond default page size (32/32)")
    test_paginates_across_multiple_fetches()
    print("PASS: multi-page pagination loop (page size 10)")
