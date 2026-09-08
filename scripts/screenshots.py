#!/usr/bin/env python3
"""Drive the app through a full day on a phone-sized viewport and save
screenshots for the README. Needs a running server (default :8765) and
Playwright with Chromium."""
import asyncio, sys, re
from pathlib import Path
from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/screens"); OUT.mkdir(parents=True, exist_ok=True)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True,
                                        color_scheme="dark")
        page = await ctx.new_page()
        await page.goto(BASE); await page.wait_for_selector(".pin input")
        await page.screenshot(path=OUT / "01-login.png")
        for i, d in enumerate("2222"):
            await page.locator(".pin input").nth(i).fill(d)
        await page.wait_for_selector("h1:has-text('Today')")
        await page.screenshot(path=OUT / "02-today.png")

        async def say(text):
            await page.goto(BASE + "/#chat"); await page.wait_for_selector("#txt")
            await page.fill("#txt", text); await page.press("#txt", "Enter")
            await page.wait_for_function("!document.querySelector('#msgs .msg.bot:last-child')?.textContent.endsWith('…')", timeout=30000)
            await page.wait_for_timeout(400)

        async def approve():
            btn = page.locator("[data-aid] [data-act=approve]").last
            await btn.click(); await page.wait_for_timeout(700)

        await say("Chaudhry Farms ko 20 urea aur 5 dap bhej do")
        await page.screenshot(path=OUT / "03-chat-order-approval.png")
        await approve()
        txt = await page.locator("#msgs").inner_text()
        oid = re.search(r"ORD-[A-Z0-9]+", txt).group(0)
        await say(f"confirm {oid}"); await approve()
        await say(f"allocate {oid} at WH-MULTAN"); await approve()
        await say("suggest dispatch for today")
        await page.screenshot(path=OUT / "04-chat-dispatch-suggest.png")
        await say(f"dispatch plan R-MULTAN-N V-01 {oid}"); await approve()
        txt = await page.locator("#msgs").inner_text()
        did = re.search(r"DSP-[A-Z0-9]+", txt).group(0)
        await say(f"approve {did}"); await approve()

        await page.goto(BASE + "/#plan/" + did); await page.wait_for_selector("h1")
        await page.screenshot(path=OUT / "05-plan-stops.png")
        otp = re.search(r"OTP (\d{4})", await page.locator("#view").inner_text()).group(1)

        # driver closes the stop
        await page.evaluate("localStorage.clear()"); await page.goto(BASE + "/#login"); await page.wait_for_selector(".pin input")
        for i, d in enumerate("3333"):
            await page.locator(".pin input").nth(i).fill(d)
        await page.wait_for_selector("h1:has-text('Today')")
        await page.goto(BASE + "/#driver"); await page.wait_for_selector(".item.tap")
        await page.screenshot(path=OUT / "06-driver-stops.png")
        await page.locator(".item.tap").first.click(); await page.wait_for_selector("#cf")
        await page.fill("input[name=cash]", "50000"); await page.fill("input[name=otp]", otp)
        await page.screenshot(path=OUT / "07-driver-close-stop.png")
        await page.click("#cf button[type=submit]"); await page.wait_for_timeout(800)

        # clerk closes the day
        await page.evaluate("localStorage.clear()"); await page.goto(BASE + "/#login"); await page.wait_for_selector(".pin input")
        for i, d in enumerate("2222"):
            await page.locator(".pin input").nth(i).fill(d)
        await page.wait_for_selector("h1:has-text('Today')")
        await say(f"{did} driver handed 45000"); await approve()
        await page.screenshot(path=OUT / "08-chat-reconcile.png")
        await say("remind everyone over 30 days"); await approve()
        await page.goto(BASE + "/#khata"); await page.wait_for_selector("#klist .item")
        await page.screenshot(path=OUT / "09-khata.png")
        await page.goto(BASE + "/#reminders"); await page.wait_for_selector("#rl .item")
        await page.screenshot(path=OUT / "10-reminders.png")
        await page.goto(BASE + "/#today"); await page.wait_for_selector(".tile")
        await page.screenshot(path=OUT / "11-today-end.png")
        await page.goto(BASE + "/#audit"); await page.wait_for_selector("#al .item")
        await page.screenshot(path=OUT / "12-audit.png")
        await browser.close()
        print("ok", sorted(x.name for x in OUT.iterdir()))

asyncio.run(main())
