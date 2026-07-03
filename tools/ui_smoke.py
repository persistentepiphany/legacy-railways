"""Headless browser pass over the cockpit UI (frontend/live via /live/).

Waits out the boot recompute, then walks the new surfaces: headline
dropdown → Revenue ledger → Network tab (expand a row) → Statistics tab
(move the eligible-share slider) → provenance step expand. Screenshots
land in /tmp/ui_smoke/.

Run:  .venv/bin/python tools/ui_smoke.py [base_url]
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SHOTS = Path("/tmp/ui_smoke")
SHOTS.mkdir(exist_ok=True)

results: list[tuple[str, str]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ("OK" if ok else "FAIL") + (" " + detail if detail else "")))
    print(f"[{'OK' if ok else 'FAIL'}] {name} {detail}")


def shot(page, name: str) -> None:
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=False)


def body_text(page) -> str:
    return page.evaluate("document.body.innerText")


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1680, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(BASE + "/live/", wait_until="domcontentloaded")
    page.wait_for_selector("text=PROVENANCE", timeout=120_000)
    step("boot", True)

    # -- lifecycle: nothing renders numbers until the analyst explicitly runs
    #    impact (single-verdict-source rule), so click Run impact FIRST, then
    #    wait for the affected count and the verdict strip together. ---------
    try:
        page.get_by_text("Run impact", exact=True).first.click()
    except Exception as e:  # noqa: BLE001
        step("run-impact-click", False, str(e)[:100])

    deadline = time.time() + 300
    n_affected = 0
    while time.time() < deadline:
        m = re.search(r"(\d+)\s*\n?affected fares", body_text(page))
        if m and int(m.group(1)) > 0:
            n_affected = int(m.group(1))
            break
        time.sleep(3)
    step("impact-computed", n_affected > 0, f"affected={n_affected}")
    time.sleep(2)

    # the map's empty-hint card stays mounted at opacity:0 (fade survives), so
    # check the verdict strip's empty-state copy instead of the raw text.
    ok = "the change's verdict lands here" not in body_text(page)
    step("run-impact-verdict", ok)
    time.sleep(1)
    shot(page, "10-verdict")

    # -- headline dropdown (the £ figure with the ▾ chevron) -----------------
    try:
        tgt = page.locator("text=▾").last
        tgt.click(force=True)
        time.sleep(1.5)
        shot(page, "11-headline-drop")
        txt = body_text(page)
        opened = ("basis" in txt.lower() and ("exposure" in txt.lower() or "journey" in txt.lower()))
        step("headline-dropdown", opened)
        if opened:
            tgt.click(force=True)
            time.sleep(0.5)
    except Exception as e:  # noqa: BLE001
        step("headline-dropdown", False, str(e)[:100])

    # -- Revenue tab + penny-exact ledger footer -----------------------------
    try:
        # .last — the left-rail ANALYSIS MODULES row is also labelled "Revenue";
        # clicking it would toggle the module off instead of opening the tab.
        page.get_by_text("Revenue", exact=True).last.click()
        time.sleep(2)
        shot(page, "12-revenue-ledger")
        txt = body_text(page)
        has_sum = ("Σ Δ" in txt) or ("to the penny" in txt)
        step("revenue-ledger", has_sum)
    except Exception as e:  # noqa: BLE001
        step("revenue-ledger", False, str(e)[:100])

    # -- Network tab -----------------------------------------------------------
    try:
        page.get_by_text("Network", exact=True).first.click()
        time.sleep(3)
        shot(page, "13-network")
        txt = body_text(page)
        ok = "Manchester" in txt and re.search(r"aberration|inversion", txt, re.I) is not None
        step("network-tab", ok)
        page.locator("text=Manchester – London Euston").last.click(force=True)
        time.sleep(1.5)
        shot(page, "14-network-expanded")
        step("network-expand", True)
    except Exception as e:  # noqa: BLE001
        step("network-tab", False, str(e)[:100])

    # -- Statistics tab + eligible-share slider ---------------------------------
    try:
        page.get_by_text("Statistics", exact=True).first.click()
        time.sleep(6)  # stats forces demand/carbon/revenue_odm includes → refetch
        shot(page, "15-stats")
        before = body_text(page)
        slider = page.locator("input[type=range]").first
        slider.evaluate("(el) => { el.value = 40; el.dispatchEvent(new Event('input', {bubbles:true})); }")
        time.sleep(8)
        shot(page, "16-stats-slider-40")
        after = body_text(page)
        step("stats-slider", before != after and "40" in after,
             "content changed" if before != after else "no change")
    except Exception as e:  # noqa: BLE001
        step("stats-slider", False, str(e)[:100])

    # -- provenance step expand ---------------------------------------------------
    try:
        card = page.locator("text=flow lookup").first
        card.click(force=True)
        time.sleep(1.5)
        shot(page, "17-provenance-expanded")
        step("provenance-expand", True)
    except Exception as e:  # noqa: BLE001
        step("provenance-expand", False, str(e)[:100])

    print("\npage JS errors:", errors[:6] if errors else "none")
    browser.close()

print("\n== SUMMARY ==")
for name, r in results:
    print(f"  {r:<8} {name}")
