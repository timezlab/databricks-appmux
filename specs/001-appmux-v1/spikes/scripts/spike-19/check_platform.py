"""Spike 19, Free Edition: load the export served by the `appmux-spike-19` app through the Databricks Apps proxy in headless
Chromium and record CSP violations, whether the page rendered and polled, and the response headers of the page.

usage: check_platform.py <chromium-headless-shell>
environment: APP_URL (the app's URL), BEARER (a workspace user's token; sent as `Authorization: Bearer` on every request to the app,
read here and never printed). Output: one JSON line. Needs `playwright` for Python.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE_PATH = "/_appmux/"
MIN_POLLS = 4
WAIT_S = 20


def main() -> None:
    app_url = os.environ["APP_URL"].rstrip("/")
    bearer = os.environ["BEARER"]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=sys.argv[1], headless=True, args=["--no-sandbox"])
        context = browser.new_context(extra_http_headers={"Authorization": "Bearer " + bearer})
        page = context.new_page()
        page.add_init_script(
            "window.__csp = []; document.addEventListener('securitypolicyviolation', e => window.__csp.push("
            "{directive: e.violatedDirective, blocked: String(e.blockedURI).slice(0, 60)}));"
        )
        failures = []
        page.on("requestfailed", lambda r: failures.append(r.url.replace(app_url, "<app-url>")[:90]))
        response = page.goto(app_url + BASE_PATH, timeout=60000)
        headers = {k: v for k, v in response.all_headers().items() if k in ("content-security-policy", "content-type", "x-content-type-options")}
        policy = re.sub(r"'sha256-[A-Za-z0-9+/=]+'", "'sha256-…'", headers.get("content-security-policy", ""))
        deadline = time.time() + WAIT_S
        polls = 0
        while time.time() < deadline:
            text = page.inner_text("#polls") if page.query_selector("#polls") else "0"
            polls = int(text) if text.isdigit() else 0
            if polls >= MIN_POLLS:
                break
            time.sleep(0.25)
        violations = page.evaluate("window.__csp")
        print(json.dumps({
            "http_status": response.status, "policy_seen_by_the_browser": policy,
            "hash_count": len(re.findall(r"'sha256-", headers.get("content-security-policy", ""))),
            "rendered": page.query_selector("#status") is not None,
            "status_text": page.inner_text("#status") if page.query_selector("#status") else None,
            "polls_seen": polls, "csp_violations": len(violations), "violation_examples": violations[:3],
            "failed_requests": failures[:3],
        }), flush=True)
        browser.close()


if __name__ == "__main__":
    main()
