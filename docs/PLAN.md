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

## Marketing's taxonomy (from Marketing's channel sheet)

Marketing classifies every initiative the same way. These lists are **reference data** (editable tables, not hard-coded), because they will grow.

| List | Values |
|---|---|
| **Channel** | Paid Search, Paid Social, SEO, Content Marketing, Email Marketing, Marketing Automation, **Events**, Partnerships, Affiliates, Influencer Marketing, PR / Communications, Outbound Marketing, Product-Led Growth, Marketplace, Other |
| **Sub-channel** | Google Ads, Bing Ads, LinkedIn Ads, Meta Ads, X (Twitter) Ads, YouTube Ads, Reddit Ads, TikTok Ads, Display / Programmatic, Organic Search, Blog, Guest Posts, Email Newsletter, Email Nurture, Cold Email, Webinars, Virtual Events, **In-Person Events**, Sponsorships, Referrals, Resellers, Review Sites (G2, Capterra), App Marketplace, Community, Other |
| **Funnel stage** | Awareness, Consideration, Conversion, Retention |
| **Goal** | Traffic, Engagement, Brand Awareness, Leads, MQLs, SQLs, Trials, Demos, Customers, Revenue, Retention, Upsell, Expansion |
| **Primary KPI** | Impressions, CTR, CPC, CPM, CPL, CP-MQL, CP-SQL, CP-Trial, CAC, Conversion Rate, Pipeline Generated, Revenue, ROI, LTV/CAC |
| **Performance rating** | High, Medium, Low, Test |
| **Scale or cut** | Scale, Optimize, Maintain, Pause, Stop, Test More |
| **Brand** | Othership, DeskFlex, WorkAware, PsPortal, Tillerstack, K9Sky, Jadian, Pace+, Zoo Office, TBD/All |

**Rule 1 — funnel logic must align** (enforced on save):

| Funnel stage | Allowed goals |
|---|---|
| Awareness | Traffic, Engagement, Brand Awareness |
| Consideration | Leads, MQLs, SQLs |
| Conversion | Trials, Demos, Customers, Revenue |
| Retention | Retention, Expansion, Upsell |

**Rule 2 — performance rating:** High = strong ROI, good CAC, scalable · Medium = works but needs optimization · Low = inefficient or poor quality · Test = new / not enough data yet.

**Rule 3 — scale or cut:** Scale = increase budget & effort · Optimize = fix targeting, creative, funnel · Maintain = keep as-is · Pause = temporarily stop · Stop = kill permanently · Test More = not enough data.

**Consequence for the data model:** an event is not a separate kind of thing — it is an initiative whose channel is **Events** (sub-channel In-Person Events, Virtual Events, Webinars or Sponsorships). So events and campaigns share **one** core record (brand, channel, sub-channel, funnel stage, goal, primary KPI, target, budget, costs, outcomes, rating, decision). Physical events add their own details (venue, city, country, dates, event code) in a linked table. "Physical events first" = initiatives with channel Events / sub-channel In-Person Events.

## "Was it worth it?" — evaluation design

Uses Marketing's own framework above. Each initiative carries its own settings, because success differs per campaign:

| Setting | Meaning | Example |
|---|---|---|
| **Funnel stage + goal** | What it was for (must satisfy Rule 1) | Consideration → Leads |
| **Primary KPI + target** | The number that means success | CPL ≤ $100 |
| **Maturity window** | Days before the rating is final (deals close late) | 180 days |
| **Revenue basis** | Which revenue counts for ROI | Closed revenue (v1); weighted pipeline and LTV later |

**Performance rating** — the system **suggests** one from actual vs target:
- **High** — target met (or beaten)
- **Medium** — within 20% of target
- **Low** — missed by more than 20%
- **Test** — still inside the maturity window, or not enough data

**Scale or cut** — the **decision**, made by a person (Marketing), recorded with who decided and when. The system can suggest a default (High → Scale, Medium → Optimize, Low → Pause/Stop, Test → Test More), but the decision is theirs.

Always shown alongside, whatever the KPI: total cost, the cost-per-outcome ladder (impression → click/visitor → lead → MQL → SQL → trial/demo → customer), pipeline generated, revenue and ROI.

**Defaults by sub-channel** pre-fill the settings (e.g. In-Person Events → Conversion / Demos / CP-SQL target, 180 days); Marketing can override.

**Later options:** weighted-pipeline and LTV revenue bases (LTV/CAC ≥ 3 is a common benchmark); comparison with similar initiatives and the prior year.

*Open: confirm the suggestion thresholds (e.g. "within 20%") and the default settings per sub-channel with Robert and Tom.*

## Task list

Each task is small and built end to end (database → backend → web → mobile where relevant), then reviewed before the next.

### Groundwork
- [x] **G1** — Fix `postgresql_db/schema_metadata.py` drift so `alembic check` is clean; create the baseline migration; run migrations in the reset scripts; fail CI on drift.
- [x] **G1b** — Reference data: tables and seed values for brands, channels, sub-channels, funnel stages, goals (with Rule 1 links), primary KPIs, performance ratings and scale-or-cut options; read-only list API.
- [ ] **G2** — Events navigation item and permissions; empty Events page (web) and tab (mobile).
- [ ] **G3** — Install Android Studio and create the emulator.

### Events (physical)
- [ ] **E1** — List events: shared initiative record (name, brand, channel = Events, sub-channel, funnel stage, goal, primary KPI, owner, budget + currency, status) plus physical-event details (event code, dates, venue, city, country, region); list API filtered to In-Person Events; web list; sample data.
- [ ] **E2** — Create an event (web form, validation; status starts Planned).
- [ ] **E3** — Event detail page.
- [ ] **E4** — Edit an event and change status (Planned → Active → Completed / Cancelled), recorded in the audit log.
- [ ] **E5** — Event costs: cost lines by category (booth, materials, staff time, travel, other), planned vs actual; travel-cost file upload matched by event code.
- [ ] **E6** — Event outcomes: visitors, leads, demos, follow-ups, customers, revenue — manual entry, with source tracking ready for HubSpot.
- [ ] **E7** — "Worth it" view: evaluation settings (funnel stage, goal, primary KPI, target, maturity window, revenue basis), cost-per-outcome ladder, ROI, suggested performance rating, and the recorded scale-or-cut decision.
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

1. Do physical events need a finer type within In-Person Events (trade show, conference, roadshow, customer event, …)?
2. Full list of regions? (Brands are now known — see taxonomy.)
3. Reporting currency, and where exchange rates come from?
4. Do events already have codes (Excel / HubSpot)? Which identifier will the travel system use?
5. "Worth it" suggestion thresholds and default settings per sub-channel — to review with Robert and Tom.
6. Confirm expense request / approval workflows are out of scope.
7. The CEO asked for cost per **visitor**, **demo** and **follow-up**; the Primary KPI list has none of these (it has CP-Trial, CP-SQL). Add CP-Visitor, CP-Demo, CP-Follow-up, or map demo → SQL?
8. Which Primary KPIs are allowed for each goal (e.g. Leads → CPL, Customers → CAC)? Not in the sheet yet.
9. Confirm which sub-channels belong to which channel. The sheet lists them separately; the current links are proposed in migration `898548e5a993` (e.g. Events → In-Person Events, Virtual Events, Webinars, Sponsorships; "Other" under every channel).

## Local development

| Service | Port |
|---|---|
| Backend (FastAPI) | 8006 |
| Web (Vite) | 5177 |
| Mobile bundler (Expo Metro) | 8091 |
| Local Postgres (Docker, `marketing_portal_dev`) | 3006 |
