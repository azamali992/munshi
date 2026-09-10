# Pricing and unit economics

## What the customer compares us to

A distributor's alternatives are not "another app". They are: a second clerk
(Rs 35–50k/month in Punjab), desktop distribution software (Rs 60–200k one-off plus a
trained operator), and doing nothing — which costs them the cash and stock that leak
silently. A short deposit of Rs 5,000 twice a week is Rs 40,000 a month; one wrong
credit decision is a lakh. Munshi is priced as a fraction of the clerk it replaces and a
fraction of the leakage it stops, not against a free khata app.

## Plans

| Plan | Price (PKR / month) | For | Includes |
|---|---|---|---|
| **Dukaan** | 0 | one owner, trying it | 1 user, chat with the stub model, khata, orders, reports, Excel export. No drivers, no WhatsApp sending. |
| **Munshi** | 4,000 | one office, one or two vans | up to 6 users (any roles), all seven agents, hosted LLM, WhatsApp delivery codes/invoices/reminders (fair use 1,500 msgs/mo), daily backup, digest. |
| **Distributor** | 9,000 | multi-godown, multi-van | up to 20 users, 3 godowns, priority support on WhatsApp, monthly Excel pack, 5,000 msgs/mo. |
| **Self-hosted** | 25,000 / year | firms with their own PC/IT | the same product on their machine, updates and support, no per-user limit. |

Annual: two months free. Setup: Rs 5,000 one-off for the Excel import and a 90-minute
onboarding call — waived if they import themselves.

## Cost to serve (per Munshi-plan customer, per month)

| Item | PKR | Notes |
|---|---|---|
| Hosting share | 150 | one 2-vCPU VPS (~Rs 6,000) comfortably serves 40+ businesses (see load test) |
| LLM | 250 | ~600 chat turns × ~1.5k tokens on a Llama-class hosted model; the stub handles the rest |
| WhatsApp | 800 | ~1,500 utility messages × $0.0053 ≈ Rs 2,200 at list; realistic use is a third of the cap |
| Support | 600 | 20 min/month of a support person at scale |
| Payment gateway | 120 | ~3 % on Rs 4,000 |
| **Total** | **~1,900** | |

Gross margin on the Rs 4,000 plan ≈ 52 %; on Distributor ≈ 70 % (messages don't scale
with price). Self-hosted is ~90 % margin after the first year.

## Payback for the customer

For a distributor doing Rs 30 lakh/month with 150 accounts, the documented wins are:
one short deposit caught per fortnight (Rs 10,000/mo), reminders going out on time
(collections up 3–5 % on a Rs 15 lakh receivable book — Rs 45–75k/mo of cash earlier),
and a clerk-day a week saved on re-entry and reconciliation (Rs 8–10k/mo). Against a
Rs 4,000 plan the payback is inside the first month; the pitch is *"it pays for itself
the first time a driver's cash comes up short."*

## Targets

Year 1: 120 paying businesses, blended ARPU Rs 5,200 → Rs 7.5 lakh MRR, 55 % gross
margin. Churn assumption 3 %/month (the khata apps saw far higher, but they were free;
paid + onboarding call + owner's data inside is sticky). CAC target Rs 12,000
(one field visit + demo + import), payback 4–5 months.
