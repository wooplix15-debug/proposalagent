"""Optional Playwright check of a saved real draft in the local review UI.

Install Playwright separately, then use --thread-id from a case paused at
draft_review. This uses installed Chrome and does not re-run model agents.
"""
import argparse
import json

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("localStorage.setItem('wooplix_agent_thread', " + json.dumps(args.thread_id) + ");")
        page.goto(args.base_url, wait_until="domcontentloaded")
        page.locator("#draftPanel").wait_for(state="visible", timeout=30000)
        assert page.get_by_text("Solution recommendations — Proposed for review", exact=True).is_visible()
        page.locator("#agentTracePanel summary").click()
        trace = page.locator("#agentTrace").inner_text()
        assert "solution_architect" in trace and "quality_critic" in trace
        page.locator('.format-btn[data-fmt="docx"]').click()
        page.locator("#reviewerName").fill("Browser smoke reviewer")
        with page.expect_download(timeout=120000) as download:
            page.locator("#approveBtn").click()
        output = download.value
        assert output.suggested_filename.endswith(".docx") and output.failure() is None
        page.locator("#resultBox").wait_for(state="visible", timeout=10000)
        assert not errors, errors
        print("Browser draft restore, specialist trace, approval and DOCX download passed.")
        print("Download:", output.suggested_filename)
        browser.close()


if __name__ == "__main__":
    main()
