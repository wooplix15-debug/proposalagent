"""Inspect the visual canvas and an existing saved case using installed Chrome."""
import argparse
from urllib.parse import quote

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--screenshot")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1100})
        page = context.new_page()
        errors, writes = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
        page.goto(f"{args.base_url}/workflow?thread_id={quote(args.thread_id, safe='')}", wait_until="domcontentloaded")
        expect(page.locator("#threadInput")).to_have_value(args.thread_id)
        expect(page.locator("#trace li").first).to_be_visible()
        assert page.locator(".node.completed").count() > 0
        page.locator('[data-node="risk"]').click()
        expect(page.locator("#detailTitle")).to_have_text("Risk & Dependency Agent")
        assert "risk_agent:" in page.locator("#trace li.match").inner_text()
        if args.screenshot:
            page.screenshot(path=args.screenshot, full_page=True)
        before = page.locator("#world").get_attribute("transform")
        page.locator("#zoomIn").click()
        assert page.locator("#world").get_attribute("transform") != before
        page.locator("#focus").click()
        page.locator("#exactMode").click()
        expect(page.locator("#exactMode")).to_have_attribute("aria-pressed", "true")
        assert page.locator(".node").count() == 26
        assert page.locator(".edge").count() == 41
        page.locator("#overviewMode").click()
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#threadInput")).to_have_value(args.thread_id)
        expect(page.locator("#trace li").first).to_be_visible()
        page.locator("#architecture").click()
        expect(page.locator("#trace li")).to_have_count(0)
        expect(page.locator(".node.completed")).to_have_count(0)
        page.locator("#threadInput").fill("nonexistent-workflow-smoke-case")
        page.locator("#loadCase").click()
        expect(page.locator("#error")).to_have_text("Agent thread not found.")
        page.locator("#architecture").click()
        expect(page.locator("#error")).to_be_hidden()
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator("#canvas")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.goto(args.base_url, wait_until="domcontentloaded")
        expect(page.locator("#workflowLink")).to_have_attribute("href", "/workflow")
        assert not errors, errors
        assert not writes, f"Workflow inspection should make no POST requests: {writes}"
        print("Visual workflow: saved progress, node inspection, exact topology, zoom, refresh, errors and mobile layout passed.")
        browser.close()


if __name__ == "__main__":
    main()
