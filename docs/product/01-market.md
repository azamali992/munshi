# Market: who Munshi is for, and why now

## The customer

Pakistan's consumer economy runs through a layer of small and mid-sized **distributors** —
the businesses that buy from a manufacturer or importer and sell on credit to retailers,
farms, workshops and clinics along fixed routes. Agri-inputs (fertilizer, seed, pesticide),
FMCG, pharma, lubricants, building materials, LPG, packaged food: the pattern is the same.
A typical operation has one owner, one or two clerks ("munshi"), one to three vans with
drivers, one or two godowns, 50–400 active accounts, and a khata (receivables ledger) that
is the single most valuable and least secure asset in the building.

There is no reliable census of these firms. Pakistan's SME base is usually put at
3–5 million enterprises; trade and distribution is the largest non-agricultural segment.
Each manufacturer of scale (fertilizer, FMCG, pharma, lubricants) runs a network of
hundreds to a few thousand distributors. A conservative addressable count for the segment
Munshi targets — route-based distributors with credit sales, at least one vehicle and at
least one clerk — is **in the tens of thousands**, before wholesalers who behave the same
way.

## What they use today

| Tool | Reality on the ground |
|---|---|
| Paper register + WhatsApp | Still the majority. Orders arrive as voice notes, khata is a book, cash is counted in the owner's head. |
| Digital khata apps (Udhaar Book, CreditBook, DigiKhata, Easy Khata) | 17.8M downloads and ~740K monthly actives by late 2022 across the big four; free; single-user; a ledger with reminders. None charged subscriptions; monetisation moved to lending, payments and BNPL. |
| B2B marketplaces (Bazaar, Dastgyr, Retailo) | Sell *to* retailers; do not run a distributor's own operation. Several scaled back after 2023. |
| Desktop distribution software (HysabOne, Candela, local Excel/VB systems) | Feature-rich (batches, schemes, GST), PC-bound, needs a trained operator, sold per installation. HysabOne cites 500+ businesses. |
| ERP (Oracle, SAP B1, Odoo, local ERPs) | For the manufacturer, not the distributor. |

The gap is clear: the free apps stop at the ledger; the serious software lives on a PC
behind a clerk who has to be trained; nothing does the *whole* loop — order → godown →
delivery → cash → khata → collections — on the phones the business already runs on.

## Why now

1. **Language models made the clerk's interface obsolete.** A distributor never wanted
   forms; he wanted to say "Chaudhry Farms ko 20 urea bhej do" and have it done. That is
   now a solved problem for a hosted model, and Munshi's deterministic control flow means
   the model only decides *what* to call, never whether it is allowed to.
2. **WhatsApp is the operating system.** Utility messages cost about $0.005 in Pakistan;
   a reminder, an invoice or an OTP delivered there costs less than a rupee.
3. **Smartphones are universal, PCs are not.** The driver, the salesman and the owner
   all carry Android phones. A PWA installs in one tap with no store listing.
4. **Trust is the product.** The reason owners don't hand the phone to a clerk is fear of
   silent mistakes and theft. An app where nothing moves stock or money without an
   approval card, and where every delivery is closed by the customer's own OTP, sells
   itself to exactly that fear.

## Where the money is

Khata apps proved the segment will not pay for a ledger. Distributors *do* pay — today,
in cash, for desktop software, for a second clerk, and for the leakage they never see.
Munshi is priced against the thing it replaces (a part-time clerk's salary and the
cash that goes missing), not against a free app; see `04-pricing-and-unit-economics.md`.

## Sources

- Data Darbar, *The impressive growth trajectory of Pakistani khata apps* (downloads, MAU, funding, monetisation).
- HysabOne, *FMCG distribution software in Pakistan* (feature expectations, pain points, customer count).
- Ominiflow, *Pakistan WhatsApp API pricing 2026* (indicative per-message rates; confirm on Meta's page).
- Trade.gov, *Pakistan distribution and sales channels*.
