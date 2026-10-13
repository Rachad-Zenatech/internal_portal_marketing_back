# Marketing Portal — Plan

Last updated: 2026-10-13

## Goal

Answer one question for every marketing **event** and **campaign**: **was it worth it?**

That means putting what it **cost** next to what it **produced**, and turning the two into cost-per-outcome figures, ROI and a verdict. Summary KPIs later feed the CEO dashboard.

Example (from the CEO): a $20,000 trade show with 200 leads, 20 demos and 2 customers costs $100 per lead, $1,000 per demo and $10,000 per customer. If each customer brings in $10,000, the show only broke even.

## Scope

**In scope**
- Events — **physical / in-person only for now** (trade shows, conferences, booths). Virtual events come later.
- Campaigns — starting with paid ads, imported from Marketing's existing spreadsheet.
- Costs, outcomes and the "worth it" verdict for both.
- Budget vs actual vs prior year.
- Social media (later): post metrics per brand/channel, and social spend vs budget.
- Web app plus mobile app (Android/iOS).

**Out of scope**
- **Travel management.** A separate travel management system handles booking and approval (Navan, Gigi/Phil). This platform only receives travel **costs** for events.
- Expense request / approval workflows and auto-approval rules. *(To confirm with Rachad/Ibraheem.)*
- Card and subscription governance.

## Data sources

| Data | Source now | Source later |
|---|---|---|
| Travel costs per event | File upload (CSV/Excel), matched by **event code** | Feed from the travel management system |
| Other event costs (booth, materials, staff time, …) | Manual entry | Import / finance system |
| Outcomes (visitors, leads, demos, follow-ups, customers, revenue) | **Both** manual entry **and** HubSpot | Same |
| Paid-ads campaign spend & performance | Excel import (Robert's paid-ads sheet) | Google Ads / Bing / LinkedIn APIs |

Rules:
- Every cost line and outcome value stores its **source** (manual, upload, HubSpot, feed) and when it was recorded.
- Where both a HubSpot value and a manual value exist, show the HubSpot value with the manual adjustment visible — neither silently overwrites the other.
- The **event code** is unique and stable, because uploads (and later the travel system) match on it. *Open: confirm which identifier the travel system will use.*

## "Was it worth it?" — evaluation design

What counts as success differs per event/campaign, so each one carries its own evaluation settings:

| Setting | Meaning | Example |
|---|---|---|
| **Primary goal** | What the event was for | Awareness, Lead generation, Pipeline, Revenue |
| **Target** | The number that means success for that goal | Cost per lead ≤ $100 |
| **Maturity window** | Days before the verdict is final (deals close late) | 180 days |
| **Revenue basis** | Which revenue counts for ROI | Closed revenue (v1); weighted pipeline and lifetime value later |

Goal → metric:

| Goal | Judged on |
|---|---|
| Awareness | Cost per visitor / impression |
| Lead generation | Cost per lead |
| Pipeline | Cost per demo / SQL |
| Revenue | ROI (revenue ÷ cost) |

**Verdict:** ✅ hit target · ⚠️ within 20% of target · ❌ missed target · ⏳ maturing (inside the maturity window).

Always shown alongside the verdict, whatever the goal: total cost, the cost-per-outcome ladder (visitor → lead → demo → follow-up → customer) and ROI.

**Defaults by event type** pre-fill the settings (e.g. trade show → Pipeline, cost per demo ≤ $1,000, 180 days); Marketing can override per event.

**Later options:** weighted-pipeline and customer-lifetime-value revenue bases (LTV ≥ 3× acquisition cost is a common benchmark); comparison with similar events and the prior year; a weighted multi-metric scorecard.

*Open: review these options with Robert and Tom — they decide what "worth it" means.*

## Task list

Each task is small and built end to end (database → backend → web → mobile where relevant), then reviewed before the next.

### Groundwork
- [ ] **G1** — Fix `postgresql_db/schema_metadata.py` drift so `alembic check` is clean; create the baseline migration.
- [ ] **G2** — Events navigation item and permissions; empty Events page (web) and tab (mobile).
- [ ] **G3** — Install Android Studio and create the emulator.

### Events (physical)
- [ ] **E1** — List events: `events` table (code, name, type, dates, venue, city, country, brand, region, owner, department, budget + currency, status, `format` = in-person), list API, web list, sample data.
- [ ] **E2** — Create an event (web form, validation; status starts Planned).
- [ ] **E3** — Event detail page.
- [ ] **E4** — Edit an event and change status (Planned → Active → Completed / Cancelled), recorded in the audit log.
- [ ] **E5** — Event costs: cost lines by category (booth, materials, staff time, travel, other), planned vs actual; travel-cost file upload matched by event code.
- [ ] **E6** — Event outcomes: visitors, leads, demos, follow-ups, customers, revenue — manual entry, with source tracking ready for HubSpot.
- [ ] **E7** — "Worth it" view: evaluation settings (goal, target, maturity window, revenue basis), cost-per-outcome ladder, ROI and verdict.
- [ ] **E8** — Search and filters (brand, region, status, type, dates).
- [ ] **E9** — Events on mobile: list, detail, verdict.

### Afterwards (in order)
1. **Campaigns** — same cost/outcome/verdict model; Excel import of the paid-ads sheet.
2. **Budget vs actual vs prior year** across events and campaigns.
3. **HubSpot** outcomes integration.
4. **Social media** — post metrics and social spend.
5. **CEO dashboard feed** (RabbitMQ outbox + marketing connector in the CEO backend).
6. **Travel system feed** replacing the travel-cost upload.
7. **Microsoft sign-in on mobile.**
8. **Virtual events.**

## Reference: Marketing's paid-ads spreadsheet

Columns: Brand, Market/Region, Period Start, Period End, Channel, Sub-Channel, Campaign Name, Primary KPI, Budget Allocated, Actual Spend, Impressions, Clicks, CTR, CPC, Ads Conv., HS contact, Ads Conv Rate, CPL, MQLs, SQLs, Last Update, Status, Report Link.

- Brands seen: Othership, DeskFlex, WorkAware. Regions: London, North America, Australia.
- Mixed currencies (£ and $) — store currency on every amount.
- CTR, CPC, conversion rate and CPL are calculated by the system, never imported.
- "HS contact" = HubSpot contacts = leads (CPL = spend ÷ HS contacts).

## Open questions

1. Event types Marketing runs (trade show, conference, sponsorship, roadshow, customer event, …)?
2. Full list of brands and regions?
3. Reporting currency, and where exchange rates come from?
4. Do events already have codes (Excel / HubSpot)? Which identifier will the travel system use?
5. "Worth it" defaults per event type — to review with Robert and Tom.
6. Confirm expense request / approval workflows are out of scope.

## Local development

| Service | Port |
|---|---|
| Backend (FastAPI) | 8006 |
| Web (Vite) | 5177 |
| Mobile bundler (Expo Metro) | 8091 |
| Local Postgres (Docker, `marketing_portal_dev`) | 3006 |
