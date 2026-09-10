"""Invoices, receipts and statements: the paper a customer actually gets.
Each document has an HTML form (printable, shareable by link) and a PDF
form (reportlab, attachable). Public links are HMAC-signed so a customer
can open their invoice without an account and nobody can enumerate them."""
from __future__ import annotations

import hashlib
import hmac
import html
import io
from dataclasses import asdict

from munshi.domain.repository import MunshiRepository, NotFoundError


def sign(secret: bytes, entry_id: str) -> str:
    return hmac.new(secret, entry_id.encode(), hashlib.sha256).hexdigest()[:16]


def verify(secret: bytes, entry_id: str, sig: str) -> bool:
    return hmac.compare_digest(sign(secret, entry_id), sig)


def invoice_data(repo: MunshiRepository, entry_id: str) -> dict:
    """Everything the document needs, from the ledger entry (invoice or receipt)."""
    e = repo.get_ledger_entry(entry_id)
    cust = repo.get_customer(e.customer_id)
    biz = repo.settings()
    lines: list[dict] = []
    stop = None
    if e.kind == "invoice" and e.ref.startswith("ORD-"):
        try:
            order = repo.get_order(e.ref)
            st = repo._one("SELECT * FROM stops WHERE order_id=? AND status IN ('delivered','short') ORDER BY closed_at DESC", (e.ref,))
            if st:
                import json
                delivered = {d["sku"]: int(d["qty"]) for d in json.loads(st["delivered_items"])}
                stop = dict(st)
            else:
                delivered = {i.sku: i.qty for i in order.items}
            for it in order.items:
                q = delivered.get(it.sku, 0)
                if q:
                    p = repo.get_product(it.sku)
                    lines.append({"sku": it.sku, "name": p.name, "unit": p.unit, "qty": q, "unit_price": it.unit_price, "total": round(q * it.unit_price, 2)})
        except NotFoundError:
            pass
    balance = repo.outstanding(cust.customer_id)
    return {"entry": asdict(e), "kind": "Invoice" if e.kind == "invoice" else "Receipt" if e.kind == "payment" else "Credit note",
            "customer": asdict(cust), "business": biz, "lines": lines, "amount": abs(e.amount), "balance_after": balance,
            "paid_on_delivery": (stop or {}).get("cash_collected", 0) if stop else 0}


def render_html(d: dict, public_url: str = "") -> str:
    b, c, e = d["business"], d["customer"], d["entry"]
    esc = html.escape
    rows = "".join(f"<tr><td>{esc(l['name'])}<br><small>{esc(l['sku'])}</small></td><td class=n>{l['qty']} {esc(l['unit'])}</td><td class=n>{l['unit_price']:,.0f}</td><td class=n>{l['total']:,.0f}</td></tr>" for l in d["lines"])
    lines_block = f"<table><thead><tr><th>Item</th><th class=n>Qty</th><th class=n>Rate</th><th class=n>Amount</th></tr></thead><tbody>{rows}</tbody></table>" if d["lines"] else ""
    ref = esc(e["ref"] or "")
    method = f"<p><b>Method:</b> {esc(e['method'])}</p>" if e.get("method") and e["kind"] == "payment" else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(d['kind'])} {esc(e['entry_id'])} · {esc(b['business_name'])}</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f2ec;color:#1b1b1b}}
.sheet{{max-width:680px;margin:0 auto;background:#fff;padding:28px 26px 34px}}
h1{{font-size:22px;margin:0}} h2{{font-size:15px;margin:22px 0 6px;color:#555;text-transform:uppercase;letter-spacing:.06em}}
.head{{display:flex;justify-content:space-between;gap:16px;border-bottom:3px solid #1b1b1b;padding-bottom:12px}}
.muted{{color:#666;font-size:13px}} table{{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px}}
th,td{{padding:8px 6px;border-bottom:1px solid #e5e5e5;text-align:left;vertical-align:top}} th{{font-size:12px;color:#666;text-transform:uppercase}}
.n{{text-align:right;font-variant-numeric:tabular-nums}} .total{{font-size:20px;font-weight:700;text-align:right;margin-top:10px}}
.badge{{display:inline-block;background:#1b1b1b;color:#fff;font-size:12px;padding:3px 8px;border-radius:4px;letter-spacing:.06em}}
.foot{{margin-top:26px;font-size:12px;color:#666;border-top:1px solid #e5e5e5;padding-top:10px}}
@media print{{body{{background:#fff}}.sheet{{padding:0}}}}
</style></head><body><div class="sheet">
<div class="head"><div><h1>{esc(b['business_name'])}</h1><div class="muted">{esc(b.get('city',''))}{' · ' + esc(b['phone']) if b.get('phone') else ''}</div></div>
<div style="text-align:right"><span class="badge">{esc(d['kind']).upper()}</span><div style="font-weight:700;margin-top:6px">{esc(e['entry_id'])}</div><div class="muted">{esc(e['created_at'][:10])}</div></div></div>
<h2>Customer</h2><div><b>{esc(c['name'])}</b> · {esc(c['customer_id'])}<br><span class="muted">{esc(c.get('address') or '')}{' · ' if c.get('address') else ''}{esc(c['phone'])}</span></div>
{lines_block}
<div class="total">{esc(d['kind'])} amount: Rs {d['amount']:,.0f}</div>
{f"<p class=muted>Paid on delivery: Rs {d['paid_on_delivery']:,.0f}</p>" if d['paid_on_delivery'] else ''}
{method}
{f"<p class=muted>Ref: {ref}</p>" if ref else ''}
{f"<p class=muted>Due: {esc(e['due_date'])}</p>" if e.get('due_date') and e['kind'] == 'invoice' else ''}
<p><b>Balance on account after this {esc(d['kind']).lower()}: Rs {d['balance_after']:,.0f}</b></p>
<div class="foot">Generated by Munshi for {esc(b['business_name'])}. {('Verify online: ' + esc(public_url)) if public_url else ''}</div>
</div></body></html>"""


def render_pdf(d: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    b, c, e = d["business"], d["customer"], d["entry"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm, title=f"{d['kind']} {e['entry_id']}")
    ss = getSampleStyleSheet()
    h = ParagraphStyle("h", parent=ss["Title"], fontSize=18, alignment=0, spaceAfter=2)
    muted = ParagraphStyle("m", parent=ss["Normal"], textColor=colors.HexColor("#666666"), fontSize=9)
    body = ss["Normal"]
    story = [Paragraph(html.escape(b["business_name"]), h), Paragraph(html.escape(f"{b.get('city', '')} {b.get('phone', '')}".strip()), muted), Spacer(1, 6),
             Paragraph(f"<b>{d['kind'].upper()} {html.escape(e['entry_id'])}</b> · {e['created_at'][:10]}", body), Spacer(1, 10),
             Paragraph(f"<b>{html.escape(c['name'])}</b> ({c['customer_id']})", body), Paragraph(html.escape(f"{c.get('address') or ''} {c['phone']}".strip()), muted), Spacer(1, 10)]
    if d["lines"]:
        data = [["Item", "Qty", "Rate", "Amount"]] + [[f"{l['name']} ({l['sku']})", f"{l['qty']} {l['unit']}", f"{l['unit_price']:,.0f}", f"{l['total']:,.0f}"] for l in d["lines"]]
        t = Table(data, colWidths=[90 * mm, 25 * mm, 28 * mm, 30 * mm])
        t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("LINEBELOW", (0, 0), (-1, 0), 1, colors.black),
                               ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#dddddd")), ("FONTSIZE", (0, 0), (-1, -1), 9.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        story += [t, Spacer(1, 8)]
    story.append(Paragraph(f"<b>{d['kind']} amount: Rs {d['amount']:,.0f}</b>", ParagraphStyle("t", parent=body, fontSize=13, alignment=2)))
    if d["paid_on_delivery"]: story.append(Paragraph(f"Paid on delivery: Rs {d['paid_on_delivery']:,.0f}", muted))
    if e.get("method") and e["kind"] == "payment": story.append(Paragraph(f"Method: {html.escape(e['method'])}", muted))
    if e.get("due_date") and e["kind"] == "invoice": story.append(Paragraph(f"Due: {e['due_date']}", muted))
    story += [Spacer(1, 6), Paragraph(f"<b>Balance on account: Rs {d['balance_after']:,.0f}</b>", body), Spacer(1, 14), Paragraph("Generated by Munshi.", muted)]
    doc.build(story)
    return buf.getvalue()


def whatsapp_text(d: dict, public_url: str = "") -> str:
    b, c, e = d["business"], d["customer"], d["entry"]
    if e["kind"] == "invoice":
        items = ", ".join(f"{l['qty']} {l['name']}" for l in d["lines"][:6])
        txt = f"{b['business_name']}: Invoice {e['entry_id']} for {c['name']} — Rs {d['amount']:,.0f}" + (f" ({items})" if items else "") + f". Balance: Rs {d['balance_after']:,.0f}."
    elif e["kind"] == "payment":
        txt = f"{b['business_name']}: Rs {d['amount']:,.0f} received from {c['name']} ({e.get('method') or 'cash'}). Receipt {e['entry_id']}. Balance: Rs {d['balance_after']:,.0f}. Shukriya."
    else:
        txt = f"{b['business_name']}: Credit note {e['entry_id']} of Rs {d['amount']:,.0f} applied to {c['name']}. Balance: Rs {d['balance_after']:,.0f}."
    return txt + (f" {public_url}" if public_url else "")


def statement_html(repo: MunshiRepository, customer_id: str) -> str:
    c = repo.get_customer(customer_id); b = repo.settings()
    esc = html.escape
    bal = 0.0; rows = []
    for e in repo.ledger_for(customer_id):
        bal += e.amount
        deb = f"{e.amount:,.0f}" if e.amount > 0 else ""
        cred = f"{-e.amount:,.0f}" if e.amount < 0 else ""
        rows.append(f"<tr><td>{esc(e.created_at[:10])}</td><td>{esc(e.kind.replace('_', ' '))}<br><small>{esc(e.entry_id)}</small></td><td class=n>{deb}</td><td class=n>{cred}</td><td class=n>{bal:,.0f}</td></tr>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Statement · {esc(c.name)}</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f2ec}}.sheet{{max-width:720px;margin:0 auto;background:#fff;padding:26px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:7px 5px;border-bottom:1px solid #e5e5e5;text-align:left}}.n{{text-align:right;font-variant-numeric:tabular-nums}}th{{font-size:11px;color:#666;text-transform:uppercase}}</style></head>
<body><div class="sheet"><h1 style="margin:0">{esc(b['business_name'])}</h1><p style="color:#666;margin:2px 0 14px">Statement of account · {esc(c.name)} ({esc(c.customer_id)})</p>
<table><thead><tr><th>Date</th><th>Entry</th><th class=n>Debit</th><th class=n>Credit</th><th class=n>Balance</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p style="font-size:18px;font-weight:700;text-align:right">Balance due: Rs {repo.outstanding(customer_id):,.0f}</p></div></body></html>"""


def loading_sheet_html(repo: MunshiRepository, plan_id: str) -> str:
    """The godown's printable loading list for a plan: totals per product, then stop by stop. No codes on it."""
    p = repo.get_plan(plan_id); route = repo.get_route(p.route_id); veh = repo.get_vehicle(p.vehicle_id); b = repo.settings()
    esc = html.escape
    totals: dict[str, int] = {}; stops_html = []
    for st in repo.list_stops(plan_id):
        o = repo.get_order(st.order_id); c = repo.get_customer(st.customer_id)
        for it in o.items: totals[it.sku] = totals.get(it.sku, 0) + it.qty
        lines = ", ".join(f"{it.qty} × {esc(repo.get_product(it.sku).name)}" for it in o.items)
        stops_html.append(f"<tr><td>{st.sequence}</td><td><b>{esc(c.name)}</b><br><small>{esc(c.address or '')} · {esc(c.phone)}</small></td><td>{lines}</td><td class=n>{o.total:,.0f}</td><td class=n>☐</td></tr>")
    tot_rows = "".join(f"<tr><td>{esc(repo.get_product(k).name)} <small>{esc(k)}</small></td><td class=n>{v} {esc(repo.get_product(k).unit)}</td><td class=n>☐</td></tr>" for k, v in totals.items())
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Loading sheet {esc(plan_id)}</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f2ec}}.sheet{{max-width:760px;margin:0 auto;background:#fff;padding:26px}}table{{width:100%;border-collapse:collapse;font-size:14px;margin-top:8px}}th,td{{padding:7px 6px;border-bottom:1px solid #e5e5e5;text-align:left;vertical-align:top}}.n{{text-align:right}}th{{font-size:11px;color:#666;text-transform:uppercase}}h2{{font-size:14px;color:#555;text-transform:uppercase;letter-spacing:.06em;margin:22px 0 4px}}@media print{{body{{background:#fff}}.sheet{{padding:0}}}}</style></head>
<body><div class="sheet"><h1 style="margin:0">{esc(b['business_name'])} — loading sheet</h1><p style="color:#666;margin:2px 0 6px">{esc(plan_id)} · {esc(route.name)} · {esc(veh.plate)} · {esc(p.plan_date)} · {p.load_units} units · status {esc(p.status)}</p>
<h2>Load (totals)</h2><table><thead><tr><th>Product</th><th class=n>Qty</th><th class=n>Loaded</th></tr></thead><tbody>{tot_rows}</tbody></table>
<h2>Stops</h2><table><thead><tr><th>#</th><th>Customer</th><th>Items</th><th class=n>Value</th><th class=n>Done</th></tr></thead><tbody>{''.join(stops_html)}</tbody></table>
<p style="margin-top:22px;color:#666;font-size:13px">Driver signature: ______________________ &nbsp;&nbsp; Godown: ______________________</p></div></body></html>"""
