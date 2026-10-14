# Marketing Portal — Plan

Last updated: 2026-10-13

## Goal

Answer one question for every marketing **initiative**, across **every channel**: **was it worth it?**

1. **Get every initiative onto the platform** — trade shows, Google Ads campaigns, LinkedIn pushes, email nurtures, influencer deals, … — whatever the channel.
2. **Track budgets, costs and spend** for each initiative and roll them up per channel and brand.
3. **Measure what each produced** and turn cost + results into cost-per-outcome, ROI and a verdict.
4. **Feed a summary to the CEO dashboard.**

Wherever the numbers already live in another tool (HubSpot, Google Ads, LinkedIn, Meta, …), the platform **syncs them automatically** instead of asking people to retype them.

Example (from the CEO): a $20,000 trade show with 200 leads, 20 demos and 2 customers costs $100 per lead, $1,000 per demo and $10,000 per customer. If each customer brings in $10,000, the show only broke even.

## Approach

- **One core for all channels.** An *initiative* is anything Marketing spends money on and wants to judge. Every initiative has the same core: what it is (brand, channel, sub-channel, owner, dates, status), what it's for (funnel stage → goal, primary KPI, target), what it cost (budget and spend), what it produced (funnel metrics) and the verdict.
- **One set of funnel metrics** covers every channel: impressions, clicks, visitors, engagement, leads, MQLs, SQLs, trials, demos, follow-ups, customers, pipeline, revenue. Channels differ in *which* they fill and *where the data comes from*. Ratios (CTR, CPC, CPM, CPL, CAC, ROI) are always calculated, never stored.
- **Build the core first, then one channel at a time — Events first, then Ads.** Each channel adds its own details and its data feed (spreadsheet upload first, then automatic API sync in Phase 3).
- **Sub-channels are reference data**, so new ones (e.g. a new ad platform) are a data change, not a code change. A screen for Marketing to manage them is on hold until they ask for it.

## Scope

**In scope**
- Initiatives for all 15 channels (core), with channel-specific add-ons built one channel at a time.
- Budgets, costs/spend, outcomes and the "worth it" verdict.
- Budget vs actual vs prior year, per initiative, channel and brand.
- Automatic sync from the tools Marketing already uses (see Integrations).
- CEO dashboard summary feed.
- Web app plus mobile app (Android/iOS).

**Out of scope**
- **Travel management.** A separate travel management system handles travel booking and expense approvals (per diems, Uber, hotels). This platform only receives travel **costs** and tracks them against each event.
- Expense request / approval workflows and auto-approval rules (handled by the travel management system). *(Marketing asked to confirm whether any other event costs still need approvals here.)*
- Card and subscription governance.

## Integrations (automatic sync)

| Source | What it provides | Channels |
|---|---|---|
| **HubSpot** | Contacts/leads per campaign, lifecycle stages (MQL, SQL), deals → pipeline, customers, revenue; campaign budgets/costs where tracked there | All (outcomes), any tracked in HubSpot (costs) |
| **Google Ads API** | Spend, impressions, clicks, conversions per campaign | Paid Search, YouTube Ads, Display |
| **Microsoft Advertising API** | Same, for Bing | Paid Search |
| **LinkedIn Marketing API** | Spend, impressions, clicks, leads (Lead Gen Forms) | Paid Social |
| **Meta Marketing API** | Spend, impressions, clicks, conversions | Paid Social |
| **Google Search Console / GA4** | Organic impressions, clicks, site traffic | SEO, Content |
| **Travel management system** | Travel costs per event | Events |
| **File upload (CSV/Excel)** | Anything without an API — and the fallback while API access is pending | Any |

**Sync rules**
- Each initiative is **linked to the external campaign(s)** it represents (e.g. Google Ads campaign ID, HubSpot campaign ID).
- Syncs run **on a schedule** (daily to start) plus a **"Sync now"** button; each integration shows its last sync time and errors.
- Synced values are stored **per initiative, per source, per period**, so re-syncing updates rather than duplicates.
- Every number keeps its **source** (manual, upload, HubSpot, Google Ads, …) and **when** it was recorded. A manual value never silently overwrites a synced one, or vice versa — both are visible.
- API credentials live only on the server (environment secrets), never in the browser.

**Lead time:** several of these APIs need approval before use — a Google Ads **developer token**, LinkedIn **Marketing Developer Platform** access, a Meta app review, a HubSpot private-app token from a HubSpot admin. Request these early; the file upload covers the gap.

## Marketing's taxonomy (from Marketing's channel sheet)

These lists are **reference data** (editable tables, not hard-coded), because they will grow.

| List | Values |
|---|---|
| **Channel** | Paid Search, Paid Social, SEO, Content Marketing, Email Marketing, Marketing Automation, Events, Partnerships, Affiliates, Influencer Marketing, PR / Communications, Outbound Marketing, Product-Led Growth, Marketplace, Other |
| **Sub-channel** | Google Ads, Bing Ads, LinkedIn Ads, Meta Ads, X (Twitter) Ads, YouTube Ads, Reddit Ads, TikTok Ads, Display / Programmatic, Organic Search, Blog, Guest Posts, Email Newsletter, Email Nurture, Cold Email, Webinars, Virtual Events, In-Person Events, Sponsorships, Referrals, Resellers, Review Sites (G2, Capterra), App Marketplace, Community, Other |
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

Always shown alongside, whatever the KPI: total cost, the cost-per-outcome ladder (impression → click/visitor → lead → MQL → SQL → trial/demo → customer), pipeline generated, revenue and ROI — and how it compares with other initiatives in the same channel and across channels.

**Defaults by sub-channel** pre-fill the settings (e.g. Google Ads → Consideration / Leads / CPL; In-Person Events → Conversion / Demos / CP-SQL, 180 days); Marketing can override.

*Open: confirm the suggestion thresholds (e.g. "within 20%") and the default settings per sub-channel with Marketing.*

## Task list

Each task is small and built end to end (database → backend → web → mobile where relevant), then reviewed before the next.

### Groundwork
- [x] **G1** — Fix `schema_metadata.py` drift; baseline migration; migrations in reset scripts; CI fails on drift.
- [x] **G1b** — Reference data: brands, channels, sub-channels, funnel stages, goals (Rule 1), KPIs, ratings, scale-or-cut; read-only API.
- [x] **G2** — Events navigation and permissions; empty page (web) and screen (mobile). *Becomes "Initiatives" in I1.*
- [x] **G3** — Android Studio, SDK and Pixel 7 emulator (Android 15); app running on the emulator.

Agreed phasing: **Phase 1 initiatives and events** → **Phase 2 ads** → **Phase 3 connecting external tools** (the largest piece; the whole team works on it once Marketing confirms what they want). The **travel management system** is a separate project being built in parallel; this platform only receives its costs.

### Current focus — search ads, events and the monthly budget tracker
Agreed scope for now: **search engine ads (Google Ads, Microsoft/Bing Ads)**, **events**, and a **monthly budget tracker** that replaces Marketing's budget-tracking sheet. **Initiatives are linked to monthly budgets** so spend rolls up into the right month. Budget approvals are on hold (see below).

How the pieces fit:
- A **monthly budget** = month + brand (or TBD/All) + market (+ optional channel) with an amount and currency. Example: *Sep · DeskFlex · North America · $12,000*.
- An **initiative** (a Google Ads campaign, an event, …) records **spend lines** — date, amount, currency, category, source.
- Each spend line counts against **one monthly budget**: suggested automatically from the initiative's brand + market and the spend's month, changeable by the user. An initiative that runs across months draws from each month's budget.
- The tracker **calculates** spent, remaining and progress per monthly budget — no hand-typed totals.

Tasks, in order:
- [ ] **B1** — Markets: a reference list of markets (London, North America, Germany, Australia, Korea, …) and an optional market on initiatives (form, detail, list filter).
- [ ] **I5** — Spend lines per initiative (date, amount, currency, category, source, note); add / edit / delete on the initiative page; budget vs spent in the Performance panel. *Event costs (E2) use the same spend lines with event categories.*
- [ ] **B2** — Monthly budgets: table and API; create / edit; a **budget tracker** page by month (brand, market, budget, spent, remaining, progress, notes) that looks like Marketing's sheet.
- [ ] **B3** — Link spend to monthly budgets: auto-match by month + brand + market, editable; tracker drills down to the initiatives and spend behind each budget; the initiative page shows which monthly budgets it draws from.
- [ ] **A1** — Search ads import: upload Google Ads and Microsoft Ads exports (per campaign and period: spend, impressions, clicks, conversions); map each campaign to an initiative; imported spend becomes spend lines (source = Google Ads / Microsoft Ads); CTR, CPC, conversion rate and cost per conversion calculated.
- [ ] **A2** — Search ads view: Google vs Bing per brand and market — spend, clicks, conversions, cost per conversion, against the monthly budget.
- [ ] **B4** — Carryover and transfers: unspent rolls into next month; explicit transfers between budgets (e.g. TBD/All → Othership). *Rules to confirm with Marketing first.*
- [ ] **X2a** — Google Ads and Microsoft Advertising APIs replace the A1 upload (needs developer tokens — request early).

### Phase 1 — Initiatives and events
Focus on the **Events** channel working end to end, built on the shared initiatives core — so by the end of Phase 1 an initiative can be created (or uploaded) for **any** channel. The budget and spend work is the base Phase 2 (ads) builds on.
- [x] **I1** — Initiatives list: `initiatives` table (name, brand, channel, sub-channel, owner, start/end dates, budget + currency, status); rename Events → Initiatives in navigation and permissions; list API with channel / sub-channel / brand / status filters; web list; sample data across several channels.
- [x] **I3** — Create an initiative: form for any channel; sub-channel options follow the chosen channel; funnel stage → goal enforced (Rule 1).
- [ ] **I3b** — Upload initiatives: import a spreadsheet of initiatives (any channel), with a preview and validation before saving.
- [x] **I4** — Initiative detail, edit and status (Planned → Active → Completed / Paused / Cancelled), recorded in the audit log.
- [ ] **I5** — Budget and spend: spend lines per initiative by category and period, with currency and source; budget vs spend shown.
- [x] **E1** — Event details for event initiatives, in-person and virtual in one table (no separate "hybrid"). The sub-channel decides the format — In-Person Events and Sponsorships are in-person; Virtual Events and Webinars are virtual — so there is no extra "virtual" flag to keep in sync. Shared: event code, event type, region. In-person adds venue, city, country; virtual adds platform and event link. *Done: codes, type, venue, city, country, region, and platform + event link for virtual events, on create / edit / detail / Events list.*
- [ ] **E2** — Event costs: categories (booth/space, materials, staff time, travel, other); travel-cost file upload matched by event code until the travel management system feeds it.
- [ ] **I6** — Outcomes: funnel metrics per initiative per period — manual entry, with source tracking ready for later syncs.
- [ ] **I7** — "Worth it" view: evaluation settings, cost-per-outcome ladder, ROI, suggested performance rating, recorded scale-or-cut decision.
- [ ] **I8** — Budget rollups: budget vs actual vs prior year per channel and brand.
- [ ] **I9** — Initiatives on mobile: list, detail, verdict.

### CEO dashboard
- [ ] **D1** — CEO dashboard feed: summary KPIs per channel and brand (RabbitMQ outbox + marketing connector in the CEO backend). Can start once Phase 1 data exists.

### Phase 2 — Ads
- [ ] **A1** — Paid-ads import: upload Marketing's paid-ads spreadsheet (spend, impressions, clicks, conversions, HubSpot contacts, MQLs, SQLs per campaign and period); CTR, CPC, CPL calculated.
- [ ] **A2** — Ads views: performance by sub-channel (Google, Bing, LinkedIn, Meta, …), budget vs spend, cost per lead compared across ad platforms and with other channels.

### Phase 3 — Connecting external tools *(scope to confirm with Marketing first)*
- [ ] **X1** — Integration framework: connections, external campaign links per initiative, scheduled sync jobs, "Sync now", sync status and errors.
- [ ] **X2** — Ads APIs: Google Ads, Microsoft Advertising, LinkedIn, Meta (replacing the spreadsheet upload).
- [ ] **X3** — HubSpot: leads, MQLs, SQLs, deals/pipeline, customers, revenue per campaign; costs where HubSpot tracks them.
- [ ] **X4** — Travel management system feed (replacing the travel-cost upload).
- [ ] **X5** — Others as Marketing prioritises (Search Console / GA4, email tools, …).

### On hold
- [ ] **I2** — Manage sub-channels (add, rename, deactivate; choose their channels). *Marketing hasn't asked for this — confirm before building. Until then, sub-channel changes are a small migration.*
- [ ] **B5** — Budget approvals: approval status (awaiting sign-off → approved → on hold → cancelled), effective date, change history with reasons, per-brand status inside a shared TBD/All budget, agreed local-currency amount (e.g. $10k = £7,500), "copy last month's budgets". *Replaces Marketing's approved-budgets sheet; on hold until the tracker is in use.*

### Supporting
- [ ] Microsoft sign-in on mobile.
- [ ] Fix the permission seed truncate (see Known issues) before the first deployment.

## Reference: Marketing's paid-ads spreadsheet

Columns: Brand, Market/Region, Period Start, Period End, Channel, Sub-Channel, Campaign Name, Primary KPI, Budget Allocated, Actual Spend, Impressions, Clicks, CTR, CPC, Ads Conv., HS contact, Ads Conv Rate, CPL, MQLs, SQLs, Last Update, Status, Report Link.

- Brands seen: Othership, DeskFlex, WorkAware. Regions: London, North America, Australia.
- Mixed currencies (£ and $) — store currency on every amount.
- CTR, CPC, conversion rate and CPL are calculated by the system, never imported.
- "HS contact" = HubSpot contacts = leads (CPL = spend ÷ HS contacts).

## Open questions

1. What does Marketing call these — "initiatives", "campaigns", something else? (Their sheet says "Campaign Name".)
2. Which tools does Marketing use per channel today, which do they actually want **integrated** (auto-synced), and who can grant API access (HubSpot admin, Google Ads, LinkedIn, Meta)? *Needed before Phase 3.*
2a. Do they want to add or rename sub-channels themselves (task I2)?
3. Does HubSpot hold campaign **costs/budgets**, or only leads and deals?
4. Full list of regions; reporting currency and where exchange rates come from.
5. "Worth it" suggestion thresholds and default settings per sub-channel — to review with Marketing.
6. Confirm expense request / approval workflows are out of scope.
7. The CEO asked for cost per **visitor**, **demo** and **follow-up**; the Primary KPI list has none of these. Add CP-Visitor, CP-Demo, CP-Follow-up, or map demo → SQL?
8. Which Primary KPIs are allowed for each goal (e.g. Leads → CPL, Customers → CAC)?
9. Confirm which sub-channels belong to which channel (current links proposed in migration `898548e5a993`).
10. Which roles should see Initiatives by default? Today only Super Admin has access.
11. Events: is a finer type needed (trade show, conference, roadshow, …), and do events already have codes the travel system will use?
12. Monthly budgets: only for paid ads, or do events and other channels draw from them too?
13. Carryover: does unspent always roll into next month, or only when approved? Does an overspend reduce next month?
14. A shared budget (e.g. TBD/All · North America · $25k used by WorkAware and DeskFlex): is that common, and should the tracker show each brand's share?
15. Google Ads / Microsoft Ads: one campaign per initiative, or several campaigns rolled into one? What counts as a conversion?

## Known issues (from the template)

- `postgresql_db/setup_pbac_schema.py` runs `TRUNCATE permission_modules CASCADE` on every run, which also deletes role → permission-group assignments for every role except Super Admin. Re-running it on a shared or production database would wipe permissions configured in the UI. Make the seed upsert instead of truncate before the first deployment.
- `GET /api/observability/usage/tokens` returns 500 (imports a `services.usage_service` module that the template doesn't include). No page uses it.

## Local development

| Service | Port |
|---|---|
| Backend (FastAPI) | 8006 (temporarily 8002 locally, borrowing Admin's Microsoft redirect URI) |
| Web (Vite) | 5177 |
| Mobile bundler (Expo Metro) | 8091 |
| Local Postgres (Docker, `marketing_portal_dev`) | 3006 |

Sample initiatives for local development: `python scripts/seed_sample_initiatives.py` (refuses to run against a non-local database; safe to re-run).
