#!/usr/bin/env python3
"""Drive the app through a full day as every role on a phone-sized viewport
and save screenshots for the README. Doubles as a browser-level smoke test:
any JS error or failed API call fails the run.

Needs a running server on a FRESH demo database (default :8765) and
Playwright with Chromium:  pip install playwright && playwright install chromium
"""
import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/screens"); OUT.mkdir(parents=True, exist_ok=True)
USERS = {"owner": ("0300-0000001", "1111"), "clerk": ("0300-0000002", "2222"), "driver": ("0300-0000003", "3333"), "salesman": ("0300-0000004", "4444")}
errors: list[str] = []


async def shot(page, name):
    await page.wait_for_timeout(350)
    await page.screenshot(path=OUT / f"{name}.png")


async def login(page, role):
    await page.evaluate("localStorage.clear()")
    await page.goto(BASE + "/#login"); await page.wait_for_selector("[name=phone]")
    phone, pin = USERS[role]
    await page.fill("[name=phone]", phone); await page.fill("[name=pin]", pin); await page.press("[name=pin]", "Enter")
    await page.wait_for_selector("#nav:not([hidden])", timeout=15000); await page.wait_for_timeout(500)


async def goto(page, hash_, wait_sel=None):
    await page.goto(BASE + "/#" + hash_); await page.wait_for_timeout(500)
    if wait_sel: await page.wait_for_selector(wait_sel, timeout=15000)


async def say(page, text):
    await goto(page, "chat", "#txt")
    await page.fill("#txt", text); await page.press("#txt", "Enter")
    await page.wait_for_function("!document.querySelector('#msgs .msg.bot:last-child')?.textContent.endsWith('…')", timeout=30000)
    await page.wait_for_timeout(400)


async def approve_last(page):
    await page.locator("[data-aid] [data-act=approve]").last.click(); await page.wait_for_timeout(700)


async def approve_last_as_owner(page, back_to):
    """The current (clerk) session just requested an action it can't approve itself
    (four-eyes: the demo has one clerk, so the owner is the other eligible approver).
    Switches to owner, approves via the Approvals list, then signs back in as `back_to`."""
    await login(page, "owner")
    await goto(page, "approvals", "#alist")
    await approve_last(page)
    await login(page, back_to)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, color_scheme="dark", bypass_csp=True, service_workers="block")
        page = await ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 500 else None)

        # ---- sign-in screen
        await page.goto(BASE); await page.wait_for_selector("[name=phone]"); await shot(page, "01-login")

        # ---- clerk: desk, order via chat. Four-eyes: whoever REQUESTS an order/plan can no
        # longer approve, confirm or allocate it themselves, so the owner steps in for those
        # specific clicks below (the demo has only one clerk). Screenshots stay on whichever
        # role is actually driving that click, so 04/05/06 now show the owner, not the clerk.
        await login(page, "clerk"); await shot(page, "02-clerk-desk")
        await say(page, "Chaudhry Farms ko 20 urea aur 5 dap bhej do"); await shot(page, "03-chat-order-approval")
        # the order doesn't exist yet -- it's still a pending request, no ORD- id until approved

        await login(page, "owner")   # the clerk requested this order: only the owner can approve/confirm/allocate it
        await goto(page, "approvals", "#alist"); await approve_last(page)
        await goto(page, "orders", ".item.tap")   # newest first: the one just approved
        oid = (await page.locator(".item.tap").first.get_attribute("href")).split("/")[-1]
        await goto(page, f"order/{oid}", "#confirm"); await shot(page, "04-order-detail")
        await page.click("#confirm"); await page.wait_for_selector("[data-wh]"); await page.locator("[data-wh]").first.click(); await page.wait_for_timeout(600)
        # second order from a salesman, confirmed by clerk in Approvals later
        await goto(page, "dispatch", "#suggest"); await page.click("#suggest"); await page.wait_for_selector("#sheetForm"); await shot(page, "05-dispatch-suggest")
        await page.click("#sheetForm button[type=submit]"); await page.wait_for_timeout(800)
        await page.locator("a.item.tap").first.click(); await page.wait_for_selector("#approve")
        did = re.search(r"DSP-[A-Z0-9]{8}", await page.locator("#view").inner_text()).group(0)
        await page.click("#approve"); await page.wait_for_selector("#sheetForm"); await page.click("#sheetForm button[type=submit]"); await page.wait_for_timeout(800)
        await shot(page, "06-plan-approved")
        otp = re.search(r"OTP (\d{4})", await page.locator("#view").inner_text()).group(1)

        # ---- driver: stops, close with OTP (wrong first), offline queue
        await login(page, "driver"); await shot(page, "07-driver-stops")
        await page.locator(".item.tap").first.click(); await page.wait_for_selector("#cf")
        await page.fill("input[name=cash]", "50000"); await page.fill("input[name=otp]", "0000"); await page.click("#cf button[type=submit]"); await page.wait_for_timeout(600)
        assert "OTP" in (await page.locator("#toast").inner_text()), "wrong OTP should be refused"
        await page.fill("input[name=otp]", otp); await shot(page, "08-driver-close-stop")
        await ctx.set_offline(True); await page.click("#cf button[type=submit]"); await page.wait_for_timeout(800)
        await shot(page, "09-driver-offline-queued")
        await ctx.set_offline(False); await page.evaluate("M.flushQueue()")
        await page.wait_for_function("M.state.queue.length === 0", timeout=15000); await page.wait_for_timeout(1200)
        assert "delivered" in (await page.locator("#view").inner_text()).lower(), "queued close should sync"

        # ---- salesman: book an order, log a promise
        await login(page, "salesman"); await shot(page, "10-salesman-book")
        await page.fill("[name=customer]", "Rana Brothers"); await page.dispatch_event("[name=customer]", "input")
        await page.fill(".oline .psel", "DAP 50kg (DAP-50)"); await page.fill(".oline .qty", "5"); await page.click("#bookF button[type=submit]"); await page.wait_for_timeout(800)
        await say(page, "Haji Sons promise 50000 by 2026-09-20"); await shot(page, "11-salesman-promise-waits")

        # ---- clerk: reconcile cash, receive stock, office payment, reminders
        await login(page, "clerk")
        await goto(page, "approvals", "#alist"); await shot(page, "12-approvals")
        while await page.locator("[data-aid] [data-act=approve]:not([disabled])").count():
            await approve_last(page)
        await goto(page, f"plan/{did}", "#dep"); await page.click("#dep"); await page.wait_for_selector("#sheetForm")
        await page.fill("[name=amount_counted]", "45000"); await page.click("#sheetForm button[type=submit]"); await page.wait_for_timeout(800); await shot(page, "13-reconcile-short")
        await say(page, "Received 100 urea from Fauji at 3600 bill FF-2291"); await approve_last_as_owner(page, "clerk")
        await say(page, "Chaudhry Farms paid 20000 jazzcash"); await approve_last_as_owner(page, "clerk")
        await goto(page, "chat", "#txt"); await shot(page, "14-chat-payment")
        await say(page, "Remind everyone over 30 days"); await approve_last_as_owner(page, "clerk")
        await goto(page, "reminders", "#rl"); await shot(page, "15-reminders")
        await page.locator("[data-send]").first.click(); await page.wait_for_timeout(600)
        await goto(page, "outbox", "#ol"); await shot(page, "16-outbox")
        await goto(page, "khata", "#klist"); await shot(page, "17-khata")

        # ---- owner: today, reports, credit note via chat, staff, setup, settings, Urdu
        await login(page, "owner"); await shot(page, "18-owner-today")
        await goto(page, "reports/profit", "#rb"); await shot(page, "19-report-profit")
        await goto(page, "reports/sales", "#rb"); await shot(page, "20-report-sales")
        await say(page, "Credit note Rana Brothers 5000 damaged bags"); await approve_last(page); await shot(page, "21-owner-credit-note")
        await goto(page, "staff", "#sl"); await shot(page, "22-staff")
        await goto(page, "setup", "#tpl"); await shot(page, "23-setup")
        await goto(page, "audit", "#al"); await shot(page, "24-audit")
        await goto(page, "customer/C-005", "#stmt"); await shot(page, "25-customer")
        await page.locator("a.item.tap").first.click(); await page.wait_for_selector("#pdf"); await shot(page, "26-document")
        await goto(page, "settings", "[data-lang=ur]"); await page.click("[data-lang=ur]"); await page.wait_for_timeout(1200)
        await goto(page, "today"); await page.wait_for_timeout(1200); await shot(page, "27-owner-today-urdu")
        await page.click("[href='#settings']") if await page.locator("[href='#settings']").count() else None
        await goto(page, "settings", "[data-lang=en]"); await page.click("[data-lang=en]")

        # ---- signup: a brand-new business
        await page.evaluate("localStorage.clear()"); await goto(page, "signup", "#sf"); await shot(page, "28-signup")
        await page.fill("[name=business_name]", "Karachi Agro Traders"); await page.fill("[name=city]", "Karachi"); await page.fill("[name=owner_name]", "Asif Iqbal")
        await page.fill("[name=phone]", "0321-5556667"); await page.fill("[name=pin]", "7391"); await page.uncheck("[name=sample_data]"); await page.click("#sf button[type=submit]")
        await page.wait_for_selector("#nav:not([hidden])", timeout=15000); await page.wait_for_timeout(800); await shot(page, "29-new-business-setup")

        # ---- the new business is set up entirely through forms, then runs one delivery end to end
        async def submit_sheet():
            await page.wait_for_selector("#sheetForm"); await page.click("#sheetForm button[type=submit]"); await page.wait_for_timeout(700)
        await goto(page, "products", "#add"); await page.click("#add"); await page.wait_for_selector("#sheetForm")
        await page.fill("[name=sku]", "URE-50"); await page.fill("[name=name]", "Urea 50kg"); await page.fill("[name=unit_price]", "3850"); await page.fill("[name=cost_price]", "3600"); await page.fill("[name=aliases]", "urea, yuria")
        await submit_sheet(); assert "Urea 50kg" in await page.locator("#view").inner_text()
        await goto(page, "customers", "#add"); await page.click("#add"); await page.wait_for_selector("#sheetForm")
        await page.fill("[name=name]", "Bismillah Store"); await page.fill("[name=phone]", "0301-7778889"); await page.fill("[name=address]", "Saddar"); await page.fill("[name=credit_limit]", "200000"); await page.fill("[name=opening_balance]", "15000")
        await submit_sheet(); await page.wait_for_timeout(600); assert "15,000" in await page.locator("#view").inner_text()   # lands on the customer page with the opening balance
        await goto(page, "setup", "#addV"); await page.click("#addV"); await page.wait_for_selector("#sheetForm"); await page.fill("[name=plate]", "KHI-1122"); await submit_sheet()
        await page.locator("[data-route]").first.click(); await page.wait_for_selector("#stopPick"); await page.locator("#stopPick input").first.check(); await submit_sheet()
        await page.click("#biz"); await page.wait_for_selector("#sheetForm"); await page.fill("[name=owner_phone]", "0321-5556667"); await page.fill("[name=big_order_limit]", "1000000"); await submit_sheet()
        assert "1 stops" in await page.locator("#view").inner_text()
        await goto(page, "stock", "#adj"); await page.click("#adj"); await page.wait_for_selector("#sheetForm"); await page.fill("[name=delta]", "100"); await page.fill("[name=reason]", "opening stock"); await submit_sheet()
        assert "100" in await page.locator("#view").inner_text()
        await goto(page, "staff", "#add"); await page.click("#add"); await page.wait_for_selector("#sheetForm")
        await page.fill("[name=name]", "Nadeem Driver"); await page.fill("[name=phone]", "0322-4443332"); await page.select_option("[name=role]", "driver"); await page.fill("[name=pin]", "8642"); await submit_sheet()
        assert "Nadeem" in await page.locator("#view").inner_text(); await shot(page, "30-new-business-staff")
        # order through the form, then confirm → allocate → plan → approve
        await goto(page, "orders", "#new"); await page.click("#new"); await page.wait_for_selector("#sheetForm")
        await page.fill("[name=customer]", "Bismillah Store"); await page.dispatch_event("[name=customer]", "input"); await page.fill(".oline .qty", "8"); await submit_sheet()
        await page.wait_for_selector("#editO"); await page.click("#editO"); await page.wait_for_selector("#sheetForm"); await page.fill(".oline .qty", "10"); await page.fill(".oline .price", "3800"); await submit_sheet()
        await page.wait_for_timeout(500); assert "38,000" in await page.locator("#view").inner_text()
        await page.wait_for_selector("#confirm"); await page.click("#confirm"); await page.wait_for_selector("[data-wh]"); await page.locator("[data-wh]").first.click(); await page.wait_for_timeout(600)
        await goto(page, "dispatch", "#newPlan"); await page.click("#newPlan"); await submit_sheet()
        await page.wait_for_selector("#approve"); await page.click("#approve"); await submit_sheet(); await shot(page, "31-new-business-plan")
        assert "APPROVED" in (await page.locator("#view").inner_text()).upper()
        otp2 = re.search(r"OTP (\d{4})", await page.locator("#view").inner_text()).group(1)
        # the new driver signs in and closes the stop
        USERS["driver2"] = ("0322-4443332", "8642"); await login(page, "driver2")
        await page.locator(".item.tap").first.click(); await page.wait_for_selector("#cf"); await page.fill("input[name=cash]", "38000"); await page.fill("input[name=otp]", otp2); await page.fill("input[name=note]", "all good"); await page.click("#cf button[type=submit]"); await page.wait_for_timeout(900)
        assert "delivered" in (await page.locator("#view").inner_text()).lower(); await shot(page, "32-new-business-delivered")
        # the owner records the hand-in, an expense, a supplier + purchase, and reads the profit
        USERS["owner2"] = ("0321-5556667", "7391"); await login(page, "owner2")
        await goto(page, "dispatch", "#pl"); await page.locator("a.item.tap").first.click(); await page.wait_for_selector("#dep"); await page.click("#dep"); await page.wait_for_selector("#sheetForm"); await page.fill("[name=amount_counted]", "38000"); await submit_sheet()
        await goto(page, "expenses", "#add"); await page.click("#add"); await page.wait_for_selector("#sheetForm"); await page.fill("[name=amount]", "2500"); await page.fill("[name=note]", "diesel"); await submit_sheet(); assert "2,500" in await page.locator("#view").inner_text()
        await goto(page, "suppliers", "#add"); await page.click("#add"); await page.wait_for_selector("#sheetForm"); await page.fill("[name=name]", "Engro Depot"); await submit_sheet()
        await page.click("#recv"); await page.wait_for_selector("#sheetForm"); await page.fill(".pline .psel", "Urea 50kg (URE-50)"); await page.fill(".pline .q", "50"); await page.fill(".pline .c", "3600"); await submit_sheet(); assert "180,000" in await page.locator("#view").inner_text()
        await page.locator("a.item.tap").first.click(); await page.wait_for_selector("#pay"); await page.click("#pay"); await page.wait_for_selector("#sheetForm"); await page.fill("[name=amount]", "80000"); await submit_sheet(); assert "100,000" in await page.locator("#view").inner_text()
        await goto(page, "reports/profit", "#rb"); txt = await page.locator("#view").inner_text(); assert "38,000" in txt; await shot(page, "33-new-business-profit")
        await goto(page, "audit", "#al"); assert "Asif Iqbal" in await page.locator("#view").inner_text()
        await browser.close()
    if errors:
        print("\n".join(errors)); sys.exit(1)
    print("ok", len(list(OUT.iterdir())), "screenshots")


asyncio.run(main())
