#!/usr/bin/env python3
"""Browser check for the payroll / finance / payment-proof screens (Stream C). Not collected by pytest (it needs a
running server and Chromium); run it by hand:

    MUNSHI_DATA_DIR=<fresh temp dir> MUNSHI_DEMO=1 PYTHONPATH=src python -m munshi.cli serve --port 8815
    PYTHONPATH=src:. python tests/ui_money_check.py http://127.0.0.1:8815 <out dir> [--fixtures auto|all|off]

--fixtures is the single switch between fixtures and live endpoints (tests/fixtures/money_ui.py):
  auto (default)  every request goes to the live server first; only a route the server does not have yet (FastAPI's
                  404 "Not Found" / 405) is answered from the fixtures. As Streams A, B and E land, their screens
                  switch to live by themselves.
  all             every money route is answered from the fixtures (a deterministic visual run).
  off             live only (the integration check once A, B, E and D are merged).

It drives owner and clerk through the console at 1440x900 and 820x1180, the phone (payslips, composer with a proof,
the owner's Money card, the forced change-PIN sheet) at 390x844 in English and Urdu, and one screen each in dark.
It fails on any page error, any 5xx, a missing element, or a generated PIN found anywhere it must not be (a request
URL or body, localStorage, sessionStorage, the console).
"""
from __future__ import annotations

import asyncio
import json
import struct
import sys
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent.parent / "src")]
from tests.fixtures import money_ui as fx  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "http://127.0.0.1:8815"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else "ui-money-shots")
MODE = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--fixtures=")), "auto")
OUT.mkdir(parents=True, exist_ok=True)
USERS = {"owner": ("0300-0000001", "1111"), "clerk": ("0300-0000002", "2222"), "driver": ("0300-0000003", "3333"), "salesman": ("0300-0000004", "4444")}
PINS = ("483920", "705913")                    # the fixture's generated PINs: they must never leak
errors: list[str] = []
served: dict[str, set[str]] = {"fixture": set(), "live": set()}
sent: list[tuple[str, str, str]] = []          # (method, url, body) of every request the page made
chat_bodies: list[dict] = []


def png(w: int, h: int) -> bytes:
    """A real PNG (a gradient), big enough that the client must resize it."""
    red = bytes(x * 255 // w for x in range(w))
    rows = bytearray()
    for y in range(h):
        row = bytearray(3 * w); row[0::3] = red; row[1::3] = bytes([y * 255 // h]) * w; row[2::3] = bytes([140]) * w
        rows += bytes([0]) + row

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    sig = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10])
    return sig + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")


def make_router(role_of_page: dict, state: dict):
    async def handle(route, request):
        u = urlparse(request.url)
        body, raw = None, ""
        if "multipart" in (request.headers.get("content-type") or ""):
            raw = (request.post_data_buffer or b"").decode("latin-1")     # the uploaded image: searched for a PIN too
        else:
            raw = request.post_data or ""
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = None
        sent.append((request.method, request.url, raw))
        if u.path == "/api/chat" and request.method == "POST" and body is not None:
            chat_bodies.append(body)
        hit = fx.match(request.method, u.path)
        role = role_of_page.get("role", "owner")
        scen = state.get("scenario")
        if scen == "pin" and u.path == "/api/me/pin" and request.method == "POST":
            ok = (body or {}).get("old_pin") == USERS["salesman"][1]
            return await route.fulfill(status=200 if ok else 401, json={"ok": True, "signed_out": True} if ok else {"detail": "current PIN is wrong"})
        if scen == "pin" and ((u.path == "/api/me" and request.method == "GET") or (u.path == "/api/session" and request.method == "POST")):
            resp = await route.fetch()              # Stream A adds must_change_pin to `me`; until then the check adds it
            data = await resp.json()
            (data["me"] if "me" in data else data)["must_change_pin"] = True
            return await route.fulfill(response=resp, json=data)
        if MODE == "off" or not hit:
            return await route.continue_()
        handler, m = hit
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if MODE == "auto":
            resp = await route.fetch()
            missing = resp.status == 405 or (resp.status == 404 and (await resp.text()).strip() == '{"detail":"Not Found"}')
            if not missing:
                served["live"].add(f"{request.method} {u.path}")
                return await route.fulfill(response=resp)
        status, data = handler(m, q, body, role, state)
        served["fixture"].add(f"{request.method} {u.path}")
        await route.fulfill(status=status, json=data)
    return handle


async def shot(page, name):
    await page.wait_for_timeout(450)
    await page.screenshot(path=OUT / f"{name}.png", full_page=False)


async def login_office(page, role):
    await page.goto(BASE + "/office"); await page.evaluate("localStorage.clear()")
    await page.goto(BASE + "/office"); await page.wait_for_selector("#lf [name=phone]")
    phone, pin = USERS[role]
    await page.fill("#lf [name=phone]", phone); await page.fill("#lf [name=pin]", pin); await page.press("#lf [name=pin]", "Enter")
    await page.wait_for_selector(".o-shell", timeout=15000)


async def go(page, hash_, sel):
    await page.evaluate(f"location.hash = '{hash_}'")
    await page.wait_for_timeout(350)                  # let the router swap the page before looking for `sel`
    await page.wait_for_selector(sel, timeout=15000)
    await page.wait_for_timeout(250)


async def login_phone(page, role, lang="en"):
    await page.goto(BASE + "/"); await page.evaluate("localStorage.clear()")
    await page.evaluate(f"localStorage.setItem('munshi.lang', JSON.stringify('{lang}'))")
    await page.goto(BASE + f"/?lang={lang}#login"); await page.wait_for_selector("[name=phone]")   # a new URL: a real reload, so core reads the language
    phone, pin = USERS[role]
    await page.fill("[name=phone]", phone); await page.fill("[name=pin]", pin); await page.press("[name=pin]", "Enter")
    await page.wait_for_selector("#nav:not([hidden])", timeout=15000); await page.wait_for_timeout(700)


def check(cond, msg):
    if not cond:
        errors.append("CHECK: " + msg)


async def owner_console(browser, w, h, tag, state, full=True):
    who = {"role": "owner"}
    ctx = await browser.new_context(viewport={"width": w, "height": h}, service_workers="block")
    await ctx.route("**/api/**", make_router(who, state))
    page = await ctx.new_page()
    logs: list[str] = []
    page.on("console", lambda m: logs.append(m.text))
    page.on("pageerror", lambda e: errors.append(f"pageerror [{tag}]: {e}"))
    page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 500 else None)
    await login_office(page, "owner")
    nav = await page.locator(".o-nav").inner_text()
    for lab in ("Employees", "Payroll", "Accounts & banks", "Finance", "Month close"):
        check(lab in nav, f"owner nav has {lab}")

    await go(page, "#/employees", "#tbl tbody tr"); await shot(page, f"{tag}-01-employees")
    check("Basic / rate" in await page.locator("#tbl").inner_text(), "owner sees pay columns")
    if full:
        await page.click("#addE"); await page.wait_for_selector(".o-panel")
        await page.fill(".o-panel [name=name]", "Zahid Iqbal"); await page.fill(".o-panel [name=phone]", "0301-7654321")
        await page.fill(".o-panel [name=designation]", "Driver"); await page.select_option(".o-panel [name=role_hint]", "driver")
        await page.fill(".o-panel [name=basic]", "42000"); await page.select_option(".o-panel [name=pay_method]", "bank")
        await page.check(".o-panel [name=want_login]"); await page.select_option(".o-panel [name=login_role]", "driver")
        await shot(page, f"{tag}-02-add-employee")
        await page.click(".o-panel button[type=submit]")
        await page.wait_for_selector(".o-pinbox", timeout=8000); await shot(page, f"{tag}-03-pin-once")
        check((await page.locator(".o-pin").inner_text()).replace("\n", "").replace(" ", "") == "483920", "PIN shown once in the dialog")
        wa = await page.get_attribute("#pinWa", "href")
        check(wa.startswith("https://wa.me/923017654321?text="), f"wa.me link built client-side: {wa[:40]}")
        await page.click("#pinDone"); await page.wait_for_timeout(300)
        check(await page.locator(".o-pin").count() == 0, "PIN gone after closing")
        # an existing employee: the panel, the cash exemption, end employment (cancel)
        await page.locator("#tbl tbody tr", has_text="Rafiq Hussain").click(); await page.wait_for_selector("#edetail [data-a=end]")
        await shot(page, f"{tag}-04-employee-panel")
        await page.click("#edetail [data-a=end]"); await page.wait_for_selector(".o-modal.danger"); await shot(page, f"{tag}-05-end-confirm")
        await page.keyboard.press("Escape"); await page.wait_for_timeout(200)
        check(await page.locator(".o-panel").count() == 1, "Escape closes the confirm but not the panel")
        await page.keyboard.press("Escape")

    p = fx.PERIOD
    await go(page, f"#/payroll/{p}/attendance", "#att tbody tr"); await shot(page, f"{tag}-06-attendance")
    if full:
        await page.fill("#att tbody tr:nth-child(4) [name=days_worked]", "27.5"); await page.wait_for_timeout(100)
        check(not await page.locator("#saveAtt").is_disabled(), "save enabled after a change")
        await page.fill("#att tbody tr:nth-child(4) [name=days_worked]", "30")
        await page.wait_for_timeout(100)
        check(await page.locator("#saveAtt").is_disabled(), "save blocked when days exceed the month")
        await shot(page, f"{tag}-07-attendance-over")
        await go(page, f"#/payroll/{p}/adjustments", "#addAdj"); await shot(page, f"{tag}-08-adjustments")
    await go(page, f"#/payroll/{p}/preview", ".o-st"); await shot(page, f"{tag}-09-preview")
    txt = await page.locator("#step").inner_text()
    check("296,433.88" in txt and "Not tax advice." in txt, "preview shows register totals and the boundary line")
    await go(page, f"#/payroll/{p}/approve", "#approve"); await page.click("#approve"); await page.wait_for_selector(".o-modal")
    await shot(page, f"{tag}-10-approve-confirm")
    check("Rs 296,433.88" in await page.locator(".o-modal").inner_text(), "approve confirm restates net total")
    await page.click(".o-modal button[type=submit]"); await page.wait_for_selector("#payT", timeout=10000)
    await page.click("#selAll"); await page.wait_for_timeout(150)
    rows = page.locator("#payT tbody tr")
    kashif = rows.filter(has_text="Kashif Ali")
    await kashif.locator("[name=method]").select_option("cash"); await page.wait_for_timeout(150)
    check("Refused" in await kashif.inner_text(), "cash for a non-exempt employee is refused inline")
    check(await page.locator("#review").is_disabled(), "review blocked while a row is refused")
    await shot(page, f"{tag}-11-pay-refused")
    await kashif.locator("[name=method]").select_option("bank"); await page.wait_for_timeout(150)
    await page.click("#review"); await page.wait_for_selector(".o-modal"); await shot(page, f"{tag}-12-pay-confirm")
    await page.click(".o-modal button[type=submit]"); await page.wait_for_timeout(700)
    await go(page, f"#/payroll/{p}/payslips", "[data-slip]"); await page.locator("[data-slip]").nth(1).click()
    await page.wait_for_selector(".o-slip"); await shot(page, f"{tag}-13-payslip")
    await page.keyboard.press("Escape")
    await go(page, f"#/payroll/{p}/statutory", "#st-eobi .o-st"); await shot(page, f"{tag}-14-statutory")
    if full:
        await go(page, "#/payroll/advances", "#give"); await shot(page, f"{tag}-15-advances")
        await page.click("#give"); await page.wait_for_selector(".o-panel"); await page.fill(".o-panel [name=amount]", "150000")
        await page.click(".o-panel button[type=submit]"); await page.wait_for_timeout(500)
        check("Refused under the Punjab Labour Code 2026" in await page.locator(".o-panel .formerr").inner_text(), "PLC advance cap refusal shown plainly")
        await shot(page, f"{tag}-16-advance-refused"); await page.keyboard.press("Escape")
        await go(page, "#/payroll/rates", "#swap"); await shot(page, f"{tag}-17-rates")

    await go(page, "#/accounts", ".o-acc"); await page.wait_for_selector("#book .o-st"); await shot(page, f"{tag}-18-accounts")
    await go(page, "#/accounts/ACC-HBL/reconcile", "#rt tbody tr")
    await page.fill("#sb", "490000"); await page.wait_for_timeout(100)
    check("off" in (await page.get_attribute(".o-diff .meter", "class") or ""), "difference not zero before ticking")
    for i in range(0, 4):   # rows are sorted by date; tick all but the uncleared salary payment (last, 09-30)
        await page.locator("#rt [data-i]").nth(i).check(); await page.wait_for_timeout(120)
    await page.wait_for_timeout(300)
    check("zero" in (await page.get_attribute(".o-diff .meter", "class") or ""), "difference reaches 0.00")
    await shot(page, f"{tag}-19-reconcile")
    if full:
        await go(page, "#/accounts/CASH/count", "#cc"); await page.fill("#cc [name=counted]", "45000"); await shot(page, f"{tag}-20-cash-count")

    await go(page, "#/finance", "#stbody .o-st"); await shot(page, f"{tag}-21-pnl")
    if full:
        await go(page, "#/finance/balance-sheet", "#stbody .o-st"); await shot(page, f"{tag}-22-balance-sheet")
        await go(page, "#/finance/trial-balance", "#stbody .o-st"); await shot(page, f"{tag}-23-trial-balance")
        await go(page, "#/finance/journal", "#jv"); await shot(page, f"{tag}-24-journal")
        await go(page, "#/finance/opening", "#ob"); await shot(page, f"{tag}-25-opening")
    await go(page, "#/close", "#cf"); await page.fill("#cf [name=through_date]", "2026-09-30")
    await page.click("#cf button[type=submit]"); await page.wait_for_timeout(200)
    if await page.locator(".o-modal").count() == 0:
        await page.check("#cf [name=force]"); await page.click("#cf button[type=submit]")
    await page.wait_for_selector(".o-modal.danger"); await shot(page, f"{tag}-26-close-confirm")
    await page.keyboard.press("Escape")

    for lg in logs:
        if any(pin in lg for pin in PINS):
            errors.append(f"PIN in console [{tag}]")
    storage = await page.evaluate("JSON.stringify(Object.assign({}, localStorage)) + JSON.stringify(Object.assign({}, sessionStorage))")
    if any(pin in storage for pin in PINS):
        errors.append(f"PIN in web storage [{tag}]")
    await ctx.close()


async def owner_dark(browser, state):
    ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
    await ctx.route("**/api/**", make_router({"role": "owner"}, state))
    page = await ctx.new_page(); page.on("pageerror", lambda e: errors.append(f"pageerror [dark]: {e}"))
    await login_office(page, "owner")
    await page.evaluate("localStorage.setItem('munshi.theme', JSON.stringify('dark')); window.munshiApplyTheme && window.munshiApplyTheme('dark')")
    await go(page, f"#/payroll/{fx.PERIOD}/preview", ".o-st"); await shot(page, "dark-owner-preview")
    await go(page, "#/accounts/ACC-HBL/reconcile", "#rt tbody tr"); await page.fill("#sb", "490000"); await shot(page, "dark-owner-reconcile")
    await ctx.close()


async def clerk_console(browser, w, h, tag, state):
    ctx = await browser.new_context(viewport={"width": w, "height": h}, service_workers="block")
    await ctx.route("**/api/**", make_router({"role": "clerk"}, state))
    page = await ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"pageerror [{tag}]: {e}"))
    page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 500 else None)
    await login_office(page, "clerk")
    nav = await page.locator(".o-nav").inner_text()
    check("Finance" not in nav and "Month close" not in nav, "clerk nav hides Finance and Close")
    check("Payroll" in nav and "Employees" in nav and "Accounts & banks" in nav, "clerk nav has Employees, Payroll (attendance), Accounts")
    await go(page, "#/employees", "#tbl tbody tr"); await shot(page, f"{tag}-01-employees")
    t = await page.locator("#page").inner_text()
    check("Basic / rate" not in t and "Rs " not in t, "clerk employees: no pay anywhere")
    await go(page, f"#/payroll/{fx.PERIOD}/preview", "#att tbody tr"); await shot(page, f"{tag}-02-attendance")
    t = await page.locator("#page").inner_text()
    check("Rs" not in t, "clerk payroll: no rupee on the page")
    check(await page.locator(".o-rail").count() == 0, "clerk payroll: no month rail, attendance only")
    await go(page, "#/accounts/ACC-HBL", "#book .o-st"); await shot(page, f"{tag}-03-accounts-book")
    check("Staff payments (owner only)" in await page.locator("#book").inner_text(), "clerk book redacts salary lines")
    await page.evaluate("location.hash = '#/finance'"); await page.wait_for_timeout(600)
    check("#/products" in page.url, "clerk is routed away from Finance")
    await ctx.close()


async def phone(browser, lang, state):
    tag = f"phone-{lang}"
    ctx = await browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, service_workers="block")
    await ctx.route("**/api/**", make_router({"role": "salesman"}, state))
    page = await ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"pageerror [{tag}]: {e}"))
    await login_phone(page, "salesman", lang)
    await go(page, "#more", ".list.more a[href='#payslips']"); await shot(page, f"{tag}-01-more")
    await go(page, "#payslips", ".mv-slip"); await shot(page, f"{tag}-02-payslips")
    await page.locator(".mv-slip").first.click(); await page.wait_for_selector(".dt table"); await shot(page, f"{tag}-03-payslip")
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); await shot(page, f"{tag}-04-payslip-bottom")
    # the composer: attach a large photo (resized client-side), then a PDF over 5 MB (refused before upload)
    await go(page, "#chat", "#composer .mv-attach")
    await page.click(".mv-attach"); await page.wait_for_selector(".mv-pick:not([hidden])"); await shot(page, f"{tag}-05-attach-menu")
    await page.keyboard.press("Escape")
    big = png(2400, 1800)
    await page.locator("#composer input[type=file]:not([capture])").set_input_files({"name": "receipt.png", "mimeType": "image/png", "buffer": big})
    await page.wait_for_selector(".mv-chip img", timeout=10000); await page.wait_for_timeout(1200); await shot(page, f"{tag}-06-proof-chip")
    await page.locator("#composer input[type=file]:not([capture])").set_input_files({"name": "statement.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n" + b"0" * (5 * 1024 * 1024 + 10)})
    await page.wait_for_timeout(500); await shot(page, f"{tag}-07-too-big")
    check("5 MB" in await page.locator("#toast").inner_text(), "PDF over 5 MB refused before upload")
    await page.fill("#txt", "Rana Brothers paid 20000 jazzcash" if lang == "en" else "رانا برادرز نے 20000 جاز کیش سے دیے")
    await page.press("#txt", "Enter"); await page.wait_for_timeout(2500); await shot(page, f"{tag}-08-sent-with-proof")
    check(any(b.get("attachment_ids") for b in chat_bodies), "chat POST carried attachment_ids")
    await ctx.close()


async def phone_owner(browser, lang, state):
    tag = f"phone-owner-{lang}"
    ctx = await browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, service_workers="block")
    await ctx.route("**/api/**", make_router({"role": "owner"}, state))
    page = await ctx.new_page(); page.on("pageerror", lambda e: errors.append(f"pageerror [{tag}]: {e}"))
    await login_phone(page, "owner", lang)
    await go(page, "#today", ".mv-money a, a.mv-money"); await page.locator("a.mv-money").scroll_into_view_if_needed(); await shot(page, f"{tag}-01-money-card")
    await ctx.close()


async def phone_pin(browser, state):
    ctx = await browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, service_workers="block")
    st = dict(state, scenario="pin")
    await ctx.route("**/api/**", make_router({"role": "salesman"}, st))
    page = await ctx.new_page(); page.on("pageerror", lambda e: errors.append(f"pageerror [pin]: {e}"))
    await login_phone(page, "salesman", "ur")
    await page.wait_for_selector("#mvPin", timeout=8000); await shot(page, "phone-pin-01-forced-ur")
    await page.fill("#mvPin [name=old_pin]", "9999"); await page.fill("#mvPin [name=new_pin]", "7391"); await page.fill("#mvPin [name=again]", "7391")
    await page.click("#mvPin button[type=submit]"); await page.wait_for_timeout(500); await shot(page, "phone-pin-02-wrong-old")
    check(await page.locator("#mvPin").count() == 1, "wrong old PIN keeps the sheet and the session")
    await page.fill("#mvPin [name=old_pin]", "4444"); await page.click("#mvPin button[type=submit]"); await page.wait_for_timeout(800)
    check(await page.locator("[name=phone]").count() == 1, "after a PIN change the phone is back at sign-in")
    await shot(page, "phone-pin-03-signed-out")
    await ctx.close()


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        await owner_console(browser, 1440, 900, "owner-1440", {})
        await owner_console(browser, 820, 1180, "owner-820", {}, full=False)
        await owner_dark(browser, {})
        await clerk_console(browser, 1440, 900, "clerk-1440", {})
        await clerk_console(browser, 820, 1180, "clerk-820", {})
        for lang in ("en", "ur"):
            await phone(browser, lang, {})
            await phone_owner(browser, lang, {})
        await phone_pin(browser, {})
        await browser.close()
    for method, url, body in sent:
        if any(pin in (url + (body or "")) for pin in PINS):
            errors.append(f"PIN sent in a request: {method} {url}")
    print("fixture-served:", sorted(served["fixture"]))
    print("live-served:", sorted(served["live"]))
    if errors:
        print("\n".join(errors)); sys.exit(1)
    print("ok", len(list(OUT.glob("*.png"))), "screenshots in", OUT)


asyncio.run(main())
