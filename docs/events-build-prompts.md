# Events — build prompts

One prompt = one small task, built and checked before the next. Paste them in order into Claude Code, opened in the repo named in the prompt's first line. Each prompt says what to build, where, the rules, how to test it, and what is **not** part of it.

## Progress (checked 13 Oct 2026)

| Status | Prompts | Commits |
|---|---|---|
| ✅ Done | Readiness: separate secrets, token issuer check, admin fallbacks removed; CI fails on lint/tests; audit log permission + names; python-multipart and openpyxl added | back `d22545a`, `73d0e7f` |
| ✅ Done | 1.1 Regions and event types · 1.2 Event details table with `EVT-YYYY-NNNN` codes · 1.3 Save event details on create · 1.4 Return, edit and search event details | back `808626a`, `d8443fc`, `58e4292` |
| ✅ Done | 1.5 Event fields in the form (with `CountryPicker`, `eventRules.ts`) · 1.6 Event section on the detail page, code column in the list | front `3910e2b`, `b367b10`, `11e4a4e` |
| ⏳ Uncommitted | Goal section restyle in `InitiativeFormDialog.tsx`; new "Current focus" section in `docs/PLAN.md` | — |
| ⏳ In progress | **1.7** Virtual event platform and link — back end written, not committed (migration `bdb72500c5be`) | — |

**What changed in the plan:** `PLAN.md`'s new *Current focus* is **search ads, events and a monthly budget tracker**. Spend lines (I5) now also count against **monthly budgets** (month + brand + market). So this list adds **markets (B1)**, **monthly budgets (B2)** and **linking spend to budgets (B3)** before the uploads, and the old "events summary" becomes a prior-year view inside the tracker. Search-ads prompts (A1, A2, B4, X2a) will be a separate list.

**Where the line is with the travel management system (14 Oct):** flights, hotels, Uber, per diems and **any expense a staff member pays and claims back** belong to the travel system — requests, approvals and auto-approval included. **Supplier costs for an event** — booth space, badges, lighting, carpet, furniture, shipping, printing, giveaways, sponsorship fees — belong here. Today the marketing owner tracks them in Excel and they're reconciled monthly against the annual marketing budget (Hadil, now Jim). Part 3 replaces that sheet and Part 4 replaces the monthly reconciliation. Travel costs arrive automatically from the travel system (Part 6: a push whenever a request is approved, paid or cancelled, plus a daily pull), so they can't be counted twice.

Order: **1.7 → Part 2 (markets) → Part 3 (event costs) → Part 4 (monthly budgets and reconciliation) → Part 5 (cost-sheet upload) → Part 6 (travel costs synced from the travel system) → Part 7 (last year's events) → Part 8 (results) → Part 9 (worth it) → Part 10 (prior year) → Part 11 (mobile).**

**Working alongside the travel system's developer:** both systems are built in parallel and combined later. The only connection now is Part 6 — travel costs flow into each event's costs automatically. Part 6.1 writes the contract to hand to the other developer; our side is built and tested against a fake travel system.

## Decisions these prompts assume

Open questions are in the roadmap's question log and `PLAN.md`. Until Marketing answers, the prompts use these defaults, chosen so they're cheap to change later:

| Topic | Assumed default | Question |
|---|---|---|
| Channels per initiative | One channel per initiative (as built) | Q38 |
| Event codes | The portal generates them (built). The travel system uses ours | Q35 |
| Markets vs event regions | Markets (London, North America, Germany…) go on **every** initiative; each market belongs to a region. Event `region_id` stays for now; decide later whether to derive it from the market | new — see Part 2 |
| Who uses monthly budgets | All channels can link spend to a monthly budget; the link is optional | PLAN.md Q12 |
| Required event fields | In-Person: city + country. Sponsorships: country. Virtual / Webinars: none (built) | Q17 |
| Travel and staff expenses | Only from the travel management system, synced automatically (push on approval + daily pull). The Travel category can't be entered by hand | Q44 |
| Supplier cost stages | Estimate → Committed (ordered / contract signed / deposit) → Invoiced → Paid. Each line keeps its original estimate so variance is visible | Q45 |
| Approval of supplier costs | No per-line approval in the portal for now; the marketing owner records costs and Jim reconciles monthly. Revisit with B5 budget approvals | Q31 |
| Which month a cost counts in | The invoice date (or expected date while it's an estimate) — used for the monthly budget and for reconciliation | Q46 |
| OPEX / CAPEX | Optional per line, defaulted from the category (a booth you buy and reuse may be CAPEX; space rental is OPEX) | Q14 |
| Staff time | No stored rates. A calculator creates a spend line (people × days × day rate). This is the cost of people's time, not an expense claim, so it isn't the travel system's | Q12 |
| Currency | Every amount keeps its own currency. Totals are per currency; mixed currencies are flagged, never converted | Q13 |
| Rating | High = target met; Medium = within 20%; Low = missed by > 20%; Test = inside maturity window or no data | Q08, Q09 |

## Rules that apply to every prompt

Both repos' `CLAUDE.md` already cover most of this; the prompts repeat what matters most:

- **Back end:** raw asyncpg (`pool = get_pool()` then `async with pool.acquire() as conn`), no SQL in loops (use `UNNEST`), new tables go in `postgresql_db/schema_metadata.py` **and** an Alembic autogenerate migration (CI fails on drift), audit with `services/rbac_service.log_audit(conn, ...)` inside the same transaction, permissions with `require_permission("INITIATIVES_VIEW" | "INITIATIVES_UPDATE" ...)`. Map database constraint names to readable messages like `_CONSTRAINT_MESSAGES` in `initiative_service.py`. Reference lists are served by `services/reference_data_service.py` (cached) via `GET /api/reference-data`.
- **Tests:** follow `tests/conftest.py` (`client`, `admin`, `viewer`, `ref` fixtures). Any new table that references `initiatives` uses `ON DELETE CASCADE` so the fixtures' cleanup still works; anything else a test user creates must be removed in `_delete_user`. Tests skip when the local database is down — make sure they actually ran.
- **Front end:** API calls only through `src/services/*` + `apiClient`; types in `src/types/*`; React Query hooks in `src/hooks/*` with keys from `src/services/queryKeys.ts`; shadcn components; toasts with `sonner`; gate buttons with `hasPermission("INITIATIVES_UPDATE")`. Follow the existing `useState` + `validate()` form pattern and the `Section` / `Detail` / `Stat` components and colour style already in `InitiativeDetail.tsx`. Event rules live in `src/pages/Initiatives/eventRules.ts`.
- **Checks before finishing:** back end `wsl python3 -m py_compile <touched files>`, `pytest tests/ -q`, `git diff --check`. Front end `npm run build` and `npm run lint`. Don't commit; I review and commit each task.

---

## Part 1 — Event details (finish E1)

### 1.7 Virtual event platform and link

```text
Repos: internal_portal_marketing_back, then internal_portal_marketing_front

Task: finish E1 — virtual events (sub-channels VIRTUAL_EVENTS and WEBINARS) get a platform and an event link, like in-person events have venue/city/country. PLAN.md E1 describes this; event_details already exists (migration 437cb74797c5).

Back end:
1. Add to event_details: platform VARCHAR(80) NULL (free text, e.g. "Zoom", "Teams", "ON24"), event_url VARCHAR(500) NULL with a check that it starts with http:// or https:// (constraint name event_details_event_url_check). schema_metadata.py + one autogenerate migration with downgrade.
2. EventDetailsInput / the update model: accept platform and event_url (strip, empty → None, validate the URL scheme in Pydantic too). Return platform and event_url in _SELECT / InitiativeListItem.
3. Rule: platform and event_url only make sense for virtual sub-channels. If they're sent for IN_PERSON_EVENTS or SPONSORSHIPS → 422 "Platform and event link are for virtual events." Venue/city/country stay allowed on virtual events (some webinars are hosted from an office) — don't block them.
4. Include both in the audit values. Map the new check constraint to "Event link must start with http:// or https://".
5. Tests in tests/test_events_create.py / test_events_update.py: create a webinar with platform + link; bad URL → 422; platform on an in-person event → 422; PATCH changes the link and audits it.

Front end:
6. Types: platform, event_url on InitiativeListItem and the event inputs.
7. InitiativeFormDialog: when the sub-channel is in ONLINE_EVENT_SUB_CHANNELS show Platform (text input with suggestions Zoom, Teams, Google Meet, ON24, Hopin, Other) and Event link instead of Venue/City/Country first; keep location fields available under a small "Add a location" toggle. Validate the URL in validate().
8. EventSection on InitiativeDetail: for online events show Platform and the link (opens in a new tab, rel="noopener noreferrer").

Not part of this task: anything else on the event page.
Checks: alembic upgrade/check, py_compile, pytest, git diff --check; npm run build, npm run lint.
```

---

## Part 2 — Markets (PLAN.md B1)

### 2.1 Markets reference data and market on initiatives (back end)

```text
Repo: internal_portal_marketing_back

Task: add markets — where an initiative is aimed (London, North America, Germany, Australia, Korea…) — and an optional market on every initiative. Monthly budgets will be matched on brand + market + month later, so this comes first.

Context: regions already exist (migration 660db2a20979) and are used by event_details.region_id. Markets are finer than regions and apply to all channels, not just events.

Build:
1. Reference table markets (same shape as other reference tables: id, code unique, name, sort_order, is_active, created_at) plus region_id INT NULL FK → regions.id. Seed from Marketing's sheets: LONDON "London" (UK & Europe), NORTH_AMERICA "North America" (North America), GERMANY "Germany" (UK & Europe), AUSTRALIA "Australia" (Australia & NZ), KOREA "Korea" (Asia), GLOBAL "Global / All markets" (no region). Leave a comment that the list is to be confirmed with Marketing.
2. initiatives.market_id INT NULL FK → markets.id (constraint initiatives_market_id_fkey → "Unknown market."). schema_metadata.py + one autogenerate migration with the seed and downgrade.
3. Serve markets (with region_id) in GET /api/reference-data.
4. InitiativeCreate / InitiativeUpdate accept market_id (optional, clearable). _SELECT returns market_id and market_name. Include it in audit values like the other fields (add it to _UPDATABLE_COLUMNS).
5. List filter: GET /api/initiatives?market_id=.
6. For events: if an event is created with a market but no region, default event_details.region_id to the market's region. Don't change existing data.
7. scripts/seed_sample_initiatives.py: give the sample initiatives markets (keep the script re-runnable).
8. Tests: reference data includes markets; create/update/clear market_id; unknown market → 422; list filter; event region defaults from market.

Not part of this task: UI, monthly budgets.
Checks: alembic upgrade/check, py_compile, pytest, git diff --check.
```

### 2.2 Market in the form, detail page and list (front end)

```text
Repo: internal_portal_marketing_front

Task: show and edit the market everywhere the brand appears.

Build:
1. Types: markets in src/types/referenceData.ts (with region_id); market_id / market_name on InitiativeListItem and the create/update types.
2. InitiativeFormDialog: Market select next to Brand (optional, "No specific market" option). For events, when a market is chosen and Region is empty, pre-fill Region from the market's region (don't overwrite a region the person picked).
3. InitiativeDetail: Market in the Overview details; Initiatives list: a Market column and a Market filter beside the brand filter (sends market_id).
4. Keep URL filter state consistent with the existing filters (e.g. ?channel=EVENTS&market=NORTH_AMERICA if the list already syncs filters to the URL; otherwise follow what it does today).

Checks: npm run build, npm run lint; create a Google Ads initiative with market Korea and an event with market Germany (region fills in).
```

---

## Part 3 — Event costs (PLAN.md I5, event part of E2)

Replaces the marketing owner's Excel cost sheet for each event. Spend lines are shared by every channel (search ads use them too); this part shapes them around supplier costs because events come first.

### 3.1 Cost categories and spend lines table

```text
Repo: internal_portal_marketing_back

Task: add the tables that hold every cost against an initiative, shaped for supplier costs (booth, badges, lighting, carpet…) with stages from estimate to paid. No API yet.

Boundary: travel and anything a staff member pays and claims back belong to the travel management system. Those costs only ever arrive here from that system (synced automatically — Part 6), never typed in by hand.

Build:
1. Reference table spend_categories (same shape as other reference tables) plus:
   - applies_to VARCHAR(20) NOT NULL default 'ALL', check in ('ALL','EVENTS','ADS') — so the UI shows the relevant ones
   - manual_entry_allowed BOOLEAN NOT NULL default true — false for TRAVEL
   - default_cost_type VARCHAR(5) NULL, check in ('OPEX','CAPEX')
   Seed (EVENTS unless noted): BOOTH_SPACE "Booth space", BOOTH_BUILD "Booth build & furniture" (default CAPEX — to confirm with Finance), BOOTH_SERVICES "Booth services (electrical, lighting, carpet, internet, cleaning)", REGISTRATION "Registration & badges", SHIPPING "Shipping & drayage", PRINT_SIGNAGE "Printing & signage", GIVEAWAYS "Giveaways & swag", SPONSORSHIP_FEES "Sponsorship fees", LEAD_RETRIEVAL "Lead retrieval", AV_TECH "AV & tech", HOSPITALITY "Booth catering & hospitality", TRAVEL "Travel (from the travel system)" (manual_entry_allowed false); MEDIA_ADS "Media / ads" (ADS); STAFF_TIME "Staff time", SOFTWARE "Software", AGENCY_FEES "Agency fees", OTHER "Other" (ALL). Everything not marked CAPEX defaults to OPEX. Add to GET /api/reference-data.
2. Table spend_lines:
   - id UUID pk (gen_random_uuid()), initiative_id UUID NOT NULL FK → initiatives ON DELETE CASCADE, index on initiative_id
   - category_id INT NOT NULL FK → spend_categories
   - description VARCHAR(255) NULL ("10x20 booth carpet, grey"), note TEXT NULL
   - vendor VARCHAR(255) NULL ("Freeman", "GES", the venue)
   - status VARCHAR(20) NOT NULL default 'COMMITTED', check in ('ESTIMATED','COMMITTED','INVOICED','PAID')
   - estimated_amount NUMERIC(14,2) NULL — the original estimate or quote, kept for variance
   - amount NUMERIC(14,2) NOT NULL, check amount <> 0 — the current figure: the estimate until invoiced, then the actual (negative allowed for refunds/credits)
   - currency CHAR(3) NOT NULL, check upper-case 3 letters
   - incurred_on DATE NOT NULL — the invoice date (expected date while ESTIMATED/COMMITTED). Decides which month's budget and reconciliation it falls in
   - invoice_ref VARCHAR(100) NULL; paid_on DATE NULL; payment_method VARCHAR(20) NULL check in ('AP_INVOICE','CORPORATE_CARD','OTHER')
   - check: status = 'PAID' requires paid_on (spend_lines_paid_on_check)
   - cost_type VARCHAR(5) NULL check in ('OPEX','CAPEX')
   - source VARCHAR(30) NOT NULL default 'MANUAL', check in ('MANUAL','STAFF_CALCULATOR','COST_SHEET_UPLOAD','TRAVEL_FEED','GOOGLE_ADS','MICROSOFT_ADS')
   - external_ref VARCHAR(100) NULL — the source system's id; partial unique index on (source, external_ref) where external_ref is not null
   - partial unique index on (lower(vendor), invoice_ref) where both are not null — the same supplier invoice can't be recorded twice (spend_lines_vendor_invoice_key)
   - reconciled_month DATE NULL, reconciled_at timestamptz NULL, reconciled_by UUID NULL FK users — filled by the monthly reconciliation (Part 4)
   - import_batch_id UUID NULL (FK added with the imports table in Part 5 — leave a comment)
   - monthly_budget_id UUID NULL (FK added with monthly budgets in Part 4 — leave a comment)
   - created_at/updated_at/created_by/updated_by like initiatives
3. schema_metadata.py + one autogenerate migration with the seed (hand-add the expression index on lower(vendor) if autogenerate misses it).

Not part of this task: API, UI.
Checks: upgrade/downgrade/upgrade, alembic check, py_compile, git diff --check.
```

### 3.2 Spend lines API

```text
Repo: internal_portal_marketing_back

Task: list, add, edit and delete costs for one initiative, with the stage rules.

Build (new files tools/spend_router.py, services/spend_service.py, models/spend_model.py; register the router in server.py like initiative_router):
1. GET  /api/initiatives/{id}/spend-lines — INITIATIVES_VIEW. Ordered by category sort order, then incurred_on. Include category name, status, vendor, estimate, amount, variance (amount − estimated_amount when INVOICED/PAID and an estimate exists), invoice_ref, paid_on, cost_type, source, reconciled flag, created_by name. 404 if the initiative doesn't exist.
2. POST /api/initiatives/{id}/spend-lines — INITIATIVES_UPDATE. Body: category_id, status (default COMMITTED), amount, estimated_amount (defaults to amount when status is ESTIMATED), currency (default the initiative's), incurred_on, vendor, description, invoice_ref, paid_on, payment_method, cost_type (default the category's default_cost_type), note, source (only MANUAL or STAFF_CALCULATOR here).
3. PATCH /api/spend-lines/{line_id} — INITIATIVES_UPDATE. Partial update. Moving to INVOICED or PAID keeps estimated_amount as it was, so variance appears.
4. DELETE /api/spend-lines/{line_id} — INITIATIVES_UPDATE.
5. Rules (readable 422 messages):
   - A category with manual_entry_allowed = false (Travel) can't be used from this endpoint: "Travel and staff expenses come from the travel management system."
   - PAID needs paid_on.
   - Lines from the travel system or ads imports (TRAVEL_FEED, GOOGLE_ADS, MICROSOFT_ADS) can't be edited or deleted one by one: "This cost came from the travel system / an ads import. Undo the import instead." Lines from a cost-sheet upload (COST_SHEET_UPLOAD) are editable like manual ones.
   - Reconciled lines can't change amount, currency, incurred_on or category, and can't be deleted: "This cost was reconciled for October 2026. Ask Finance to reopen it." Notes and descriptions can still change.
   - Duplicate supplier invoice (spend_lines_vendor_invoice_key): "Invoice INV-2231 from Freeman is already recorded on EVT-2026-0004." — look up which event for the message.
6. Audit every create/update/delete with entity_type "SpendLine", entity_id the line id, initiative_id in the values; same transaction. Make the audit log page show a readable name for SpendLine entries the way 73d0e7f did for initiatives.
7. Tests (tests/test_spend_lines.py): CRUD; currency and cost_type defaults; ESTIMATED copies the estimate; PAID without paid_on → 422; Travel by hand → 422; duplicate vendor + invoice → 422 naming the event (vendor match is case-insensitive); a TRAVEL_FEED line (inserted in SQL) can't be edited; a reconciled line can't change amount but can change note; viewer can read but not write; deleting the initiative removes its lines.

Not part of this task: totals, monthly budgets, UI.
Checks: py_compile, pytest, git diff --check.
```

### 3.3 Budget vs committed vs actual totals

```text
Repo: internal_portal_marketing_back

Task: the numbers for an event's cost sheet — what's planned, committed, invoiced and paid against its budget — on one endpoint and in the list.

Build:
1. GET /api/initiatives/{id}/spend-summary (INITIATIVES_VIEW) returns, in the initiative's currency (other currencies reported separately, never converted):
   - budget_amount, budget_currency
   - planned (all lines' amount), committed (COMMITTED + INVOICED + PAID), actual (INVOICED + PAID), paid (PAID), remaining (budget − committed), percent_committed, percent_actual (null without a budget)
   - by_category: [{category_id, category_name, estimated, amount, actual, variance}]
   - by_month: [{month, committed, actual}] (month of incurred_on)
   - travel_total: lines in the TRAVEL category (from the travel system), so people can see how much of the event is travel
   - other_currencies: [{currency, planned, committed, actual}] and a mixed_currencies flag
   A small fixed number of queries.
2. List endpoint: add committed_amount, actual_amount (initiative's currency) and has_mixed_currencies to every row in the same query (LATERAL or grouped subquery, never per row). Add to InitiativeListItem.
3. Tests: one line per status lands in the right totals; a refund reduces totals; variance per category; a GBP line on a USD budget goes to other_currencies; no budget → percents null; travel_total; list columns.

Not part of this task: UI.
Checks: py_compile, pytest, git diff --check.
```

### 3.4 Event cost sheet on the initiative page

```text
Repo: internal_portal_marketing_front

Task: replace the marketing owner's Excel cost sheet with a cost sheet on the event page, and show budget vs committed vs actual in the Performance panel.

Build:
1. src/types/spend.ts, src/services/spendService.ts, src/hooks/useSpend.ts (queryKeys spendLines(id), spendSummary(id)). Mutations invalidate spendLines, spendSummary, the initiative and the initiatives list.
2. InitiativeDetail: a full-width "Costs" Section (same Section/icon style as the page), laid out like a cost sheet:
   - rows grouped by category with a subtotal per category; columns Item (description), Vendor, Estimate, Actual, Variance (red when over), Status chip (Estimate / Committed / Invoiced / Paid), Invoice #, Paid on
   - a lock icon on reconciled lines ("Reconciled for Oct 2026") and on travel lines ("From the travel system"); travel rows grouped into one "Travel (from the travel system)" group
   - a totals footer: Planned · Committed · Actual · Paid
   - status filter chips
   - row actions Edit / Delete for editable lines only (hidden without INITIATIVES_UPDATE)
3. "Add cost" opens SpendLineDialog (new file in src/pages/Initiatives/): category (applies_to ALL or the channel's group, and manual_entry_allowed only), item, vendor (free text with suggestions from vendors already used), status, estimate, amount, currency, date (labelled "Expected date" for Estimate/Committed, "Invoice date" for Invoiced/Paid), invoice #, paid on + payment method (shown when Paid), OPEX/CAPEX (prefilled from the category), note. Validate like the API. Delete confirms in-page (AlertDialog).
4. A quick status action on each row ("Mark invoiced", "Mark paid") that asks for the invoice # or paid date inline.
5. Performance panel: Budget, Committed, Actual, Remaining (existing Stat component) and a two-tone bar — committed in a light tone, actual solid over it (amber ≥ 90% committed, red over 100%). Spend by category beneath. Mixed-currency warning line if any.
6. Initiatives list: a "Committed / Actual" column (amounts + thin two-tone bar; "—" without a budget).
7. Empty state: "No costs yet. Add the booth space, build, services, badges and materials as you book them — start with estimates if you only have quotes."

Not part of this task: staff calculator, uploads, monthly budgets, reconciliation.
Checks: npm run build, npm run lint; enter a realistic booth (space, build, carpet, lighting, badges) and move lines from Estimate to Paid; check over-budget and no-budget events in light and dark mode.
```

### 3.5 Staff time calculator

```text
Repo: internal_portal_marketing_front

Task: record staff time without storing anyone's salary.

Build:
1. In the Costs section, "Add staff time" (INITIATIVES_UPDATE) opens StaffTimeDialog: number of people, number of days, day rate, currency (default the initiative's), date (default the event's end date, else today), optional note.
2. Show the live total ("5 people × 3 days × $400/day = $6,000") and save one spend line: category STAFF_TIME, source STAFF_CALCULATOR, description exactly that sentence, amount = the total.
3. Staff-time lines are editable/deletable like manual ones (edit opens the normal SpendLineDialog).

Not part of this task: staff rosters or stored rates (open question Q12).
Checks: npm run build, npm run lint.
```

---

## Part 4 — Monthly budgets (PLAN.md B2, B3)

### 4.1 Monthly budgets table and API

```text
Repo: internal_portal_marketing_back

Task: monthly budgets — the rows of Marketing's budget-tracking sheet — as a table with an API. Example row: Sep 2026 · DeskFlex · North America · $12,000.

Build (tools/budget_router.py, services/budget_service.py, models/budget_model.py):
1. Table monthly_budgets: id UUID pk, month DATE NOT NULL (always the 1st — check constraint), brand_id INT NOT NULL FK → brands (TBD/All is a brand already), market_id INT NULL FK → markets (null = all markets), channel_id INT NULL FK → channels (null = any channel), amount NUMERIC(14,2) NOT NULL check >= 0, currency CHAR(3) NOT NULL, notes TEXT NULL, created/updated at/by. Unique (month, brand_id, market_id, channel_id) treating NULLs as equal (NULLS NOT DISTINCT on Postgres 16, or a unique index on coalesced values) → message "There's already a budget for that month, brand and market."
2. Add the FK spend_lines.monthly_budget_id → monthly_budgets.id ON DELETE SET NULL.
3. Permissions: add a BUDGETS navigation item and permission group following how INITIATIVES was added in ed7fce2 (setup_pbac seed — make sure it's an upsert, not the old TRUNCATE). VIEW to read, UPDATE/CREATE/DELETE to change. Until Marketing roles are set up, Super Admin has it.
4. Endpoints: GET /api/monthly-budgets?year=&month=&brand_id=&market_id= ; POST ; PATCH /{id} ; DELETE /{id} (blocked with a message if spend lines are linked: "Move or unlink the 14 costs on this budget first."). Audit entity "MonthlyBudget".
5. Tests: create/edit/delete; duplicate month+brand+market → 422 (including both markets null); month not the 1st → 422; delete blocked when linked; permissions.

Not part of this task: spent/remaining (next), linking spend, UI.
Checks: alembic upgrade/check, py_compile, pytest, git diff --check.
```

### 4.2 Link spend lines to monthly budgets

```text
Repo: internal_portal_marketing_back

Task: every spend line counts against one monthly budget — suggested automatically, changeable by the user (PLAN.md B3).

Build:
1. Matching rule (one SQL function or service query, documented): for a spend line, find the monthly budget with month = first of month of incurred_on, brand = the initiative's brand, and the most specific match on market (initiative's market, else null) and channel (initiative's channel, else null). Prefer exact market + channel, then market only, then channel only, then brand-only. No match → stays unlinked.
2. Apply it automatically when a spend line is created or its incurred_on changes, and when an initiative's brand/market/channel changes (re-match its unpinned lines in one statement). Add spend_lines.budget_link_pinned BOOLEAN NOT NULL default false: when the user chooses a budget by hand, pin it and never auto-change it.
3. PATCH /api/spend-lines/{id} accepts monthly_budget_id (null to unlink) → sets pinned = true. Validate the budget's month matches the line's month ("This cost is in October; choose an October budget.").
4. GET /api/monthly-budgets/suggest?initiative_id=&date= → the matched budget (for the UI's default).
5. Spend lines in the GET list include monthly_budget_id and a label "Oct 2026 · DeskFlex · North America".
6. Tests: each matching tier; no match; re-match after the initiative's market changes; pinned lines untouched; month mismatch rejected.

Not part of this task: tracker totals, UI.
Checks: alembic check, py_compile, pytest, git diff --check.
```

### 4.3 Tracker totals API

```text
Repo: internal_portal_marketing_back

Task: the numbers behind the budget tracker — spent, remaining and progress per monthly budget, calculated, never typed.

Build:
1. GET /api/monthly-budgets/tracker?year=2026&brand_id=&market_id= (BUDGETS_VIEW): one row per monthly budget with amount, currency, spent (sum of linked lines in the budget's currency), spent_other_currencies [{currency, amount}] (shown, not converted), remaining, percent_used, initiative_count, notes. Plus per-month totals and an "unlinked spend" figure per month (lines with no budget) so nothing goes missing. Grouped queries only.
2. GET /api/monthly-budgets/{id}/spend → the initiatives and spend lines behind one budget (for the drill-down).
3. GET /api/initiatives/{id}/budgets → the monthly budgets this initiative draws from, with how much of each it used.
4. Tests: totals with refunds; other-currency spend reported separately; unlinked spend counted; drill-down lists the right lines.

Not part of this task: UI.
Checks: py_compile, pytest, git diff --check.
```

### 4.4 Budget tracker page

```text
Repo: internal_portal_marketing_front

Task: a Budget tracker page that looks like Marketing's sheet and replaces it.

Build:
1. Route /budgets, navigation item "Budget tracker" under MARKETING, protected with navigationCode BUDGETS. Types/services/hooks for monthly budgets and the tracker.
2. Filters: year (default current), brand, market. Layout grouped by month (newest first or a month picker — follow what reads best): rows Brand · Market · Channel (if set) · Budget · Spent · Remaining · progress bar · Notes. Over budget in red; ≥ 90% amber. Month subtotal rows; an "Unlinked spend" line per month when there is some, linking to the costs that need a budget.
3. Add / edit budget dialog (month, brand, market, optional channel, amount, currency, notes) and delete with in-page confirmation — gated by BUDGETS permissions.
4. Clicking a row opens a drawer (shadcn Sheet) with the initiatives and spend lines behind it, each linking to the initiative page.
5. Empty state: "No budgets for 2026 yet. Add September's budgets to start tracking."

Checks: npm run build, npm run lint; enter two months of budgets from Marketing's sheet and compare totals with the sheet.
```

### 4.5 Budgets on the initiative page and spend line dialog

```text
Repo: internal_portal_marketing_front

Task: show which monthly budgets an initiative uses, and let people choose a budget for a cost.

Build:
1. SpendLineDialog: a "Counts against" select pre-filled from GET /monthly-budgets/suggest for the chosen date (re-query when the date changes), listing that month's budgets for the brand; "No budget" allowed. Changing it sends monthly_budget_id (the API pins it).
2. Costs table: a "Budget" column with the label; unlinked lines show "No budget" with a link to the tracker.
3. Performance panel: a small "Monthly budgets" list from GET /initiatives/{id}/budgets (Oct 2026 · DeskFlex · North America — $4,200 of $12,000).

Checks: npm run build, npm run lint.
```

---

### 4.6 Monthly reconciliation API

```text
Repo: internal_portal_marketing_back

Task: replace the monthly reconciliation Jim does in Excel — checking each month's event and marketing costs against the annual budget, and marking them as reconciled.

Build (extend tools/budget_router.py / services/budget_service.py, or a new reconciliation_service.py):
1. GET /api/reconciliation?month=2026-10-01&brand_id= (BUDGETS_VIEW): every spend line whose incurred_on is in that month, plus unreconciled lines from earlier months (carried forward, flagged "from September"). Grouped by brand → initiative (with event code). Each line: category, vendor, item, status, amount, currency, invoice_ref, paid_on, monthly budget label, reconciled flag. Per group and overall: totals by status, the month's budget total, and year-to-date budget vs actual for the brand (the "annual marketing budget" view: sum of the year's monthly budgets vs actual to date).
2. Problems listed so Jim can chase them, computed in SQL: line still ESTIMATED or COMMITTED after the event ended; INVOICED/PAID without an invoice #; PAID without a payment method; not linked to a monthly budget; currency differs from the budget's.
3. POST /api/reconciliation/reconcile (BUDGETS_UPDATE) {line_ids, month} — sets reconciled_month/at/by in ONE statement; only lines that are INVOICED or PAID can be reconciled (others listed back as skipped with the reason). POST /api/reconciliation/reopen (BUDGETS_UPDATE) {line_ids, reason} clears them; reason is required and audited.
4. Audit: one row per reconcile/reopen call (entity "Reconciliation", values: month, line count, total, reason).
5. GET /api/reconciliation/export?month= — an .xlsx (openpyxl) with one row per line and a totals sheet. Keep the column order in one constant so it can match Jim's sheet once we have a copy (Q47).
6. Tests: month filter and carry-forward; problem flags; reconcile skips ESTIMATED lines; reconciled lines then refuse amount edits (3.2 rule); reopen needs a reason; export has the expected headers; permissions (INITIATIVES_UPDATE alone can't reconcile).

Not part of this task: UI.
Checks: py_compile, pytest, git diff --check.
```

### 4.7 Monthly reconciliation page

```text
Repo: internal_portal_marketing_front

Task: the page Jim uses each month instead of the reconciliation spreadsheet.

Build:
1. Route /budgets/reconciliation, a tab or link from the Budget tracker, protected with navigationCode BUDGETS.
2. Month picker (default last month) and brand filter. Top: month budget, committed, actual, reconciled so far, and year-to-date budget vs actual per brand.
3. "Needs attention" list from the API's problems, each linking to the cost on its event page.
4. Lines grouped by brand → event with checkboxes; "Reconcile selected" (only INVOICED/PAID selectable; others show why); reconciled lines show who and when, with a "Reopen" action that asks for a reason in-page.
5. "Export to Excel" downloads the month (apiClient.downloadFile).

Checks: npm run build, npm run lint; reconcile a month of sample costs and confirm reconciled lines can't be edited on the event page.
```

---

## Part 5 — Imports: event cost sheets

### 5.1 Import groundwork: batches table and file parser

```text
Repo: internal_portal_marketing_back

Task: the shared pieces every upload will use (event cost sheets now; last year's events and search-ads exports later). No endpoint yet. python-multipart and openpyxl are already in requirements.txt.

Build:
1. Table import_batches: id UUID pk, kind VARCHAR(30) check in ('COST_SHEET','EVENTS','GOOGLE_ADS','MICROSOFT_ADS'), filename VARCHAR(255), status VARCHAR(20) check in ('PREVIEW','CONFIRMED','UNDONE','EXPIRED'), total_rows/valid_rows/error_rows/imported_rows INT, staged_rows JSONB (parsed, validated rows kept between preview and confirm), created_by UUID FK users, created_at, confirmed_at/by, undone_at/by. Add FK spend_lines.import_batch_id → import_batches.id ON DELETE SET NULL. Update tests/conftest.py _delete_user to remove the temporary user's import batches.
2. services/import_parsing.py — pure functions, no database:
   - read_table(filename, bytes) → list[dict[str, str]] for .csv (utf-8 with or without BOM) and .xlsx (first sheet, values not formulas); header names normalised (trim, case-insensitive, spaces/underscores equivalent); blank rows skipped; ImportFileError with a readable message for other types, empty files, or > 5,000 rows.
   - parse_amount(text) → Decimal | None: "$1,200.50", "£300", "(150.00)" as negative, "1 200", "." / "-" / "" as blank, Excel errors (#N/A, #DIV/0!, #VALUE!) invalid.
   - parse_date(text): ISO, DD/MM/YYYY, Excel serials/datetimes. Ambiguous formats are read as DD/MM (UK/AU usage) — document it.
   - parse_currency(text, default) → 3-letter code; $→USD, £→GBP, €→EUR, A$→AUD, C$→CAD, ₩→KRW.
3. Tests tests/test_import_parsing.py (no database needed): every case above, CSV with BOM, xlsx built in memory, too many rows, wrong file type.

Not part of this task: endpoints, UI.
Checks: alembic check, py_compile, pytest, git diff --check.
```

### 5.2 Event cost sheet upload: preview and confirm

```text
Repo: internal_portal_marketing_back

Task: let the marketing owner upload an event's existing Excel cost sheet instead of retyping it. Get a sample sheet from Marketing first (Q29) and adjust the column aliases to match it.

Columns (case-insensitive, with aliases for what their sheet calls them): Item / Description*, Category* (name or code; manual-entry categories only — Travel rows are rejected with "Travel comes from the travel management system"), Vendor, Estimate, Actual, Status (defaults: PAID when Paid On is set, INVOICED when Actual is set, otherwise ESTIMATED), Invoice #, Invoice Date / Date, Paid On, Payment Method, Currency (default the event's), OPEX/CAPEX, Notes.

Build (services/cost_sheet_import_service.py; routes in tools/import_router.py):
1. POST /api/initiatives/{id}/imports/cost-sheet (multipart, INITIATIVES_UPDATE) → preview with READY / DUPLICATE (same vendor + invoice # already recorded anywhere, or twice in the file) / INVALID rows, using import_parsing.py and import_batches (kind COST_SHEET). Amount = Actual if present, else Estimate; estimated_amount = Estimate. Rows with neither → INVALID.
2. Confirm through POST /api/imports/{batch_id}/confirm (dispatch on kind): one set-based insert with source COST_SHEET_UPLOAD and import_batch_id, then link to monthly budgets with the 4.2 rule in one statement; one audit row. Undo deletes the batch's lines unless any are reconciled ("3 of these costs are already reconciled — reopen them first.").
3. GET /api/imports/cost-sheet/template — .xlsx with the columns, an example booth (space, build, carpet, lighting, badges) and dropdowns for Category, Status, Payment Method, OPEX/CAPEX.
4. Tests: sheet with estimates only; sheet with actuals and paid dates; Travel row rejected; duplicate invoice; undo; undo blocked by a reconciled line.

Checks: alembic check (kind check constraint gains COST_SHEET), py_compile, pytest, git diff --check.
```

### 5.3 Event cost sheet upload: screen

```text
Repo: internal_portal_marketing_front

Task: the cost-sheet upload from the event page, built with reusable import components.

Build:
1. src/types/imports.ts, src/services/importsService.ts (FormData via apiClient.post; templates via apiClient.downloadFile), src/hooks/useImports.ts.
2. Reusable pieces in src/components/imports/: file drop zone (.csv/.xlsx, 5 MB), count chips, preview table shell with status filter and highlighted problem rows, confirm bar, recent uploads list with Undo. The travel and events uploads reuse them.
3. On the event's Costs section, "Upload cost sheet" opens the flow (a Sheet/drawer or /initiatives/:id/imports/cost-sheet): file + "Download template", preview (row #, status, item, category, vendor, estimate, actual, status, invoice #, messages), "Import 24 costs" / Cancel, toast with Undo.
4. Invalidate spend, spend-summary and tracker queries after confirm/undo.

Checks: npm run build, npm run lint; upload Marketing's real sheet for one past event.
```

---

## Part 6 — Travel costs from the travel management system

The two systems are built side by side by different developers and will be combined later. The only thing that crosses over for now: **travel costs flow into the event's costs automatically**. The travel system pushes a cost when a request is approved, paid, changed or cancelled; a daily pull catches anything a push missed. Our side is built and tested against the contract in 6.1, using a fake travel system, so it doesn't wait for theirs.

### 6.1 Write the travel cost contract

```text
Repo: internal_portal_marketing_back

Task: write docs/travel-cost-contract.md — the agreement we hand to the travel management system's developer. Documentation only, no code.

Cover, in plain language with JSON examples:
1. Identity: the travel system calls us with a service token (our services/service_security_policy.py: add a "travel-management" trusted application with scope "marketing.travel_costs:write" — note it, implement in 6.2). We call them with a token for scope "travel.costs:read".
2. Events: every travel request is tagged with our event code (EVT-2026-0007). Until they read our event list, staff type or pick the code; a request without a code isn't sent. (Optional later: GET /api/integrations/travel/events — our active events for their picker.)
3. One cost record = one approved request or expense line: transaction_id (their stable id, never reused), event_code, category (FLIGHT, HOTEL, GROUND_TRANSPORT, PER_DIEM, MEALS, OTHER_TRAVEL), description, amount, currency, incurred_on (travel date / expense date), status (APPROVED, PAID, CANCELLED), approved_at, paid_on, traveller count (no names — we don't need personal data), updated_at.
4. Push: POST /api/integrations/travel/costs with a batch of records whenever a request is approved, paid, changed or cancelled. Idempotent: sending the same transaction_id again updates it. Response lists accepted, updated, unmatched (unknown event code) and rejected records with reasons.
5. Pull: GET {their base url}/api/travel/costs?updated_since=<ISO time> — the same record shape, paginated; we call it once a day.
6. Status mapping on our side: APPROVED → COMMITTED, PAID → PAID, CANCELLED → removed from our totals. Amount changes after approval update the line.
7. What we never receive or store: traveller names, receipts, approvals detail — those stay in the travel system.
Add a short "questions for the travel system developer" list at the end (who assigns event codes on their side, their category list, refunds, multi-currency).

Checks: git diff --check. Share the file with the other developer before 6.2.
```

### 6.2 Receive travel costs (push)

```text
Repo: internal_portal_marketing_back

Task: the endpoint the travel system calls when travel requests are approved, paid, changed or cancelled (contract: docs/travel-cost-contract.md).

Build (tools/travel_sync_router.py, services/travel_sync_service.py, models/travel_sync_model.py):
1. Service auth: add a "travel-management" trusted application and scope "marketing.travel_costs:write" in services/service_security_policy.py, and protect the route with require_application_scope (follow how the ceo-dashboard scopes are defined; user tokens must be rejected).
2. POST /api/integrations/travel/costs, body {records: [...]} (max 1,000). In a fixed number of queries:
   - look up event codes → initiative ids
   - upsert into spend_lines on (source = 'TRAVEL_FEED', external_ref = transaction_id): category TRAVEL, status COMMITTED/PAID from the mapping, amount, currency, incurred_on, paid_on, description ("Flight — 2 travellers"); INSERT … SELECT FROM unnest(...) ON CONFLICT DO UPDATE, only when updated_at is newer than what we hold (out-of-order pushes can't overwrite newer data)
   - CANCELLED → delete the line (or mark it, if it's already reconciled: keep it and flag it for Finance, "Cancelled after reconciliation")
   - link new/changed lines to monthly budgets with the 4.2 rule in one statement
3. Unknown event codes go to a table travel_sync_unmatched (transaction_id pk, event_code, payload JSONB, first_seen_at, last_seen_at) so nothing is lost; when that event code exists later, the next sync picks them up.
4. Record each call in travel_sync_runs (id, direction PUSH/PULL, started_at, finished_at, received, inserted, updated, deleted, unmatched, rejected, error). One audit row per call.
5. Response: {accepted, updated, deleted, unmatched: [...], rejected: [{transaction_id, reason}]}.
6. Tests with a fake service token: insert, re-send same id (no duplicate), amount change, older updated_at ignored, cancel, unknown code goes to unmatched then resolves, user token rejected, 1,001 records → 422.

Checks: alembic check, py_compile, pytest, git diff --check.
```

### 6.3 Daily pull from the travel system

```text
Repo: internal_portal_marketing_back

Task: once a day, fetch travel costs changed since the last successful sync — the safety net for missed pushes.

Build:
1. Settings (env, names only in .env.example): TRAVEL_SYNC_ENABLED (default false), TRAVEL_API_BASE_URL, TRAVEL_SYNC_HOUR_UTC (default 2). Use generate_scoped_outbound_token for scope "travel.costs:read".
2. services/travel_sync_job.py: GET {base}/api/travel/costs?updated_since=<last successful sync's started_at minus 1 hour>, follow pagination, then hand the records to the same service function the push endpoint uses (one code path). Record a PULL run. Follow how graph_sync_job.py is scheduled in this repo (start on app startup, one run per day, guarded so two workers don't both run it — e.g. a Postgres advisory lock).
3. POST /api/integrations/travel/sync-now (BUDGETS_UPDATE) to run it on demand.
4. A fake travel API for tests (httpx MockTransport) returning two pages; tests: pulls and upserts, uses updated_since, a failed call records the error and doesn't move the watermark, advisory lock prevents a second concurrent run.
5. Until the travel system has a costs endpoint, TRAVEL_SYNC_ENABLED stays false; pushes still work.

Checks: py_compile, pytest, git diff --check.
```

### 6.4 Travel sync status in the portal

```text
Repo: internal_portal_marketing_front (+ a small read API in the back end)

Task: let people see that travel costs are flowing, and fix what didn't match.

Build:
1. Back end: GET /api/integrations/travel/status (BUDGETS_VIEW) → last push, last pull, last error, counts for the last 7 days; GET /api/integrations/travel/unmatched (BUDGETS_VIEW) → unmatched records (event code, description, amount, first seen).
2. Front end: on each event's Costs section, the Travel group shows "From the travel system · updated 2 hours ago". On the Budget tracker, a small "Travel sync" status (green / amber if no sync in 36 h / red on error) opening a drawer with recent runs, unmatched records ("Code EVT-2026-0099 doesn't exist — check the code in the travel system") and a "Sync now" button (BUDGETS_UPDATE).

Checks: back end py_compile/pytest; front end build/lint.
```

---

## Part 7 — Last year's events (history and the pilot)

### 7.1 Event spreadsheet import: back end

```text
Repo: internal_portal_marketing_back

Task: import Marketing's existing events spreadsheet — creates initiatives with event details and, optionally, one total cost line per category. Loads last year's events for prior-year comparisons and gives a real pilot event.

Reuse services/import_parsing.py and import_batches (kind EVENTS) and the src/components/imports pieces from 5.3. Columns: Name*, Brand*, Market, Sub-channel* (In-Person Events / Virtual Events / Webinars / Sponsorships), Start Date, End Date, Budget, Currency, Status, Owner Email, Event Type, Venue, City, Country, Region, Platform, Event Link, and optional cost columns Booth/Space, Materials, Travel, Staff Time, Other (each becomes one spend line dated the event's end date, source MANUAL, description "Imported total", linked to a monthly budget by the Part 4.2 rule).

Build:
1. POST /api/imports/events (preview), READY / INVALID / DUPLICATE: duplicate = same name + brand + start date as an existing initiative or another row. Validate reference values by name or code (case-insensitive), the sub-channel required-field rule, dates, URL; unknown owner email → the uploader, as a warning.
2. Confirm (reuse /api/imports/{batch_id}/confirm, dispatching on kind): set-based inserts of initiatives, event_details (codes generated) and spend lines in one transaction; one audit row. Undo deletes the initiatives the batch created (cascades remove details and lines) — add initiatives.import_batch_id for this.
3. GET /api/imports/events/template with dropdowns for brand, market, sub-channel, event type, region, status.
4. Tests: good file; invalid sub-channel; in-person without city; duplicate; cost columns create linked lines; undo removes everything.

Checks: alembic check, py_compile, pytest, git diff --check.
```

### 7.2 Event spreadsheet import: screen

```text
Repo: internal_portal_marketing_front

Task: the events upload screen using the components from 5.3.

Build: page src/pages/Imports/EventUpload.tsx at /initiatives/imports/events, linked from the Events list ("Import events"). Preview columns: row #, status, name, brand, market, sub-channel, dates, location or platform, budget, cost totals, messages. After confirm, toast "Imported 18 events" with Undo and a link to the Events list.

Checks: npm run build, npm run lint; import last year's events and check they appear with codes, costs and budget links.
```

---

## Part 8 — Results (PLAN.md I6)

### 8.1 Outcome metrics and outcomes table

```text
Repo: internal_portal_marketing_back

Task: tables for results (funnel metrics) per initiative per month.

Build:
1. Reference table outcome_metrics (reference shape plus unit VARCHAR(10) check in ('COUNT','MONEY')), seeded in funnel order: IMPRESSIONS, CLICKS, VISITORS, ENGAGEMENTS, LEADS, FOLLOW_UPS, MQLS, SQLS, TRIALS, DEMOS, CONVERSIONS, CUSTOMERS (COUNT); PIPELINE, REVENUE (MONEY). CONVERSIONS is what Google/Microsoft Ads report (see PLAN.md Q15). Add to GET /api/reference-data.
2. Table outcomes: id UUID pk, initiative_id FK ON DELETE CASCADE, period_start DATE NOT NULL (1st of month — check), metric_id FK, value NUMERIC(16,2) NOT NULL check >= 0, currency CHAR(3) NULL (required for MONEY metrics — enforced in the service), source VARCHAR(30) NOT NULL default 'MANUAL', recorded_at/by, unique (initiative_id, period_start, metric_id, source). Manual and synced values sit side by side.
3. schema_metadata.py + one migration.

Checks: alembic check, py_compile, git diff --check.
```

### 8.2 Results API

```text
Repo: internal_portal_marketing_back

Task: read and save results for an initiative, one month at a time.

Build (tools/outcome_router.py, services/outcome_service.py, models/outcome_model.py):
1. GET /api/initiatives/{id}/outcomes (VIEW): {periods: [...], values: [{period_start, metric_code, value, currency, source, recorded_at, recorded_by_name}]}.
2. PUT /api/initiatives/{id}/outcomes/{period_start} (UPDATE), body {values: [{metric_code, value | null, currency?}]}: upsert MANUAL values in ONE statement (unnest … ON CONFLICT DO UPDATE); null deletes that metric's manual value (one DELETE … = ANY). period_start must be the 1st (else 422). MONEY metrics default to the initiative's currency.
3. Audit one row per save: entity "Outcomes", entity_id the initiative, old/new keyed by metric code, with the period.
4. Tests: save and read; update only what was sent; null deletes; not-1st → 422; unknown metric → 422; viewer can't save; a HUBSPOT value inserted in SQL is untouched by a manual save.

Checks: py_compile, pytest, git diff --check.
```

### 8.3 Performance calculation

```text
Repo: internal_portal_marketing_back

Task: costs + results → the cost-per-outcome ladder and ROI. Calculated on request, never stored.

Build:
1. services/initiative_metrics.py — pure functions, no database:
   - Inputs: total cost (initiative's currency), counts per metric (all months; where MANUAL and a synced source both exist for the same month, prefer the synced one and report that), revenue, pipeline.
   - Outputs: cost per visitor, click, lead, follow-up, MQL, SQL, trial, demo, conversion, customer; CTR; CPC; CPM; conversion rates between consecutive ladder steps present; ROI = (revenue − cost) / cost. Division by zero or missing input → None, never an error.
2. GET /api/initiatives/{id}/performance (VIEW), fixed number of queries; costs in other currencies excluded and reported.
3. Unit tests including the CEO's example: cost 20,000, leads 200, demos 20, customers 2, revenue 20,000 → CPL 100, cost per demo 1,000, CAC 10,000, ROI 0.0. Plus zero leads, no cost, revenue without customers.

Checks: py_compile, pytest, git diff --check.
```

### 8.4 Results on the initiative page

```text
Repo: internal_portal_marketing_front

Task: enter and view results.

Build:
1. src/types/outcomes.ts, src/services/outcomesService.ts, src/hooks/useOutcomes.ts (keys outcomes(id), performance(id); saving invalidates both).
2. A "Results" Section: month picker (defaults to the start month; months with data as chips) and inputs for the metrics relevant to the channel, in funnel order. Keep "metrics per channel" in one constant: Events → Booth visitors, Leads, Follow-ups, Demos, MQLs, SQLs, Customers, Pipeline, Revenue; Paid Search/Social → Impressions, Clicks, Conversions, Leads, MQLs, SQLs, Customers, Revenue.
3. Save sends only changed values; clearing sends null. Synced values (source ≠ MANUAL) show read-only with their source. A totals row across months.

Checks: npm run build, npm run lint; enter two months for the seeded expo and check totals.
```

### 8.5 Cost ladder in the Performance panel

```text
Repo: internal_portal_marketing_front

Task: show what each outcome cost.

Build: in the Performance panel, from GET /performance, a vertical ladder — Visitor → Lead $100 → Demo $1,000 → Customer $10,000 — with counts and the conversion rate on each connector ("10% of leads → demo"); missing steps "—". Revenue, total cost and ROI underneath; ROI ≤ 0 in the warning colour with "Broke even or lost money". Note excluded other-currency costs. Pure formatting helpers in format.ts.

Checks: npm run build, npm run lint; the expo with the CEO's numbers shows $100 / $1,000 / $10,000 and ROI 0%.
```

---

## Part 9 — "Was it worth it?" (PLAN.md I7)

### 9.1 Evaluation settings and defaults by sub-channel

```text
Repo: internal_portal_marketing_back

Task: store what success means per initiative, pre-filled from sub-channel defaults.

Build:
1. Table evaluation_defaults: sub_channel_id pk FK, funnel_stage_id, goal_id, primary_kpi_id, maturity_days INT. Seed (proposals pending Marketing — Q36): In-Person Events → Conversion / Demos / CP-SQL, 180; Sponsorships → Consideration / Leads / CPL, 180; Virtual Events and Webinars → Consideration / Leads / CPL, 90; Google Ads and Bing Ads → Consideration / Leads / CPL, 90. Serve as evaluation_defaults in GET /api/reference-data.
2. Table initiative_evaluations: initiative_id pk FK ON DELETE CASCADE, target_value NUMERIC(14,2) NULL, maturity_days INT NOT NULL default 180 check 0–730, revenue_basis VARCHAR(30) NOT NULL default 'CLOSED_REVENUE' (only value for now), updated_at/by.
3. GET /api/initiatives/{id}/evaluation (VIEW) — the row, or the defaults for its sub-channel; PUT (UPDATE) upserts and audits ("Evaluation").
4. On create, if funnel stage/goal/KPI weren't sent, fill them from evaluation_defaults. Test it.
5. Tests: defaults when no row; upsert; audit; viewer can't PUT.

Checks: alembic check, py_compile, pytest, git diff --check.
```

### 9.2 Suggested rating

```text
Repo: internal_portal_marketing_back

Task: suggest High / Medium / Low / Test by comparing the primary KPI with its target.

Build:
1. In services/initiative_metrics.py:
   - KPI_DEFINITIONS: how to read each primary KPI's actual value from the performance result and whether lower or higher is better (CPL, CP-*, CAC, CPC, CPM lower; CTR, ROI, Revenue, Pipeline Generated, Impressions higher). CONVERSION_RATE and LTV_CAC unsupported → Test, "Not calculated yet".
   - suggest_rating(kpi_code, actual, target, end_date, maturity_days, today) → {rating, reason}: Test inside the window ("23 days left in the 180-day window"), or with no target or no actual; else High if met/beaten, Medium within 20%, Low beyond 20% (named constant).
2. Add {primary_kpi, actual, target, rating, reason, matures_on} to GET /performance.
3. Unit tests: both directions at exactly target, 19.9%, 20%, 20.1%; inside the window; no target; unsupported KPI.

Checks: py_compile, pytest, git diff --check.
```

### 9.3 Scale-or-cut decision

```text
Repo: internal_portal_marketing_back

Task: record Marketing's decision, keeping the history.

Build:
1. Table initiative_decisions: id UUID pk, initiative_id FK ON DELETE CASCADE, scale_or_cut_id FK, suggested_rating VARCHAR(10), note TEXT, decided_by FK users, decided_at default now().
2. POST /api/initiatives/{id}/decisions (UPDATE) {scale_or_cut_id, note} — stores the current suggestion with it; audit ("Decision"). GET (VIEW) newest first with names.
3. latest_decision on GET /performance and on list rows (one LATERAL join).
4. Tests: record and list; latest on the list; viewer can't decide.

Checks: alembic check, py_compile, pytest, git diff --check.
```

### 9.4 Verdict in the Performance panel

```text
Repo: internal_portal_marketing_front

Task: "Was it worth it?" on the initiative page.

Build:
1. Evaluation settings (editable with UPDATE): primary KPI (from the initiative's Goal), target in the KPI's unit, maturity window in days. PUT /evaluation.
2. Verdict: rating badge (High green / Medium amber / Low red / Test grey) with the reason, actual vs target ("CP-SQL $1,250 vs target $1,000 — missed by 25%"), "Rating is final on 2 May 2027" while inside the window.
3. Decision: select of scale-or-cut options with meanings, suggestion pre-selected (High → Scale, Medium → Optimize, Low → Pause, Test → Test More), note, "Record decision"; history below.
4. Page order: Overview → Event → Goal → Costs → Results → Performance (budget vs spent, cost ladder, verdict).

Checks: npm run build, npm run lint; an event inside the window, one past it that met target, one that missed.
```

### 9.5 Defaults in the form, rating in the list

```text
Repos: internal_portal_marketing_back (filter), internal_portal_marketing_front

Build:
1. Front end form: choosing a sub-channel with evaluation_defaults on CREATE pre-fills empty funnel stage/goal/KPI ("Filled in from In-Person Events defaults — change if needed"). Never overwrite choices.
2. Back end list: suggested rating per row without per-row queries — two grouped queries (costs, outcome totals) for the page's ids, then suggest_rating in Python (pages ≤ 200 rows). A `rating` filter (applied after calculation; note the limitation in a comment). Tests.
3. Front end list: Rating badge and latest Decision columns, rating filter beside status.

Checks: back end py_compile/pytest; front end build/lint.
```

---

## Part 10 — Prior year (replaces the separate events summary)

### 10.1 Prior year in the tracker and an events view

```text
Repos: internal_portal_marketing_back, then internal_portal_marketing_front

Task: budget vs actual vs prior year — the feature stakeholders called most valuable — built on the monthly budgets.

Back end:
1. Extend GET /api/monthly-budgets/tracker with prior_year_spent per row and per month: spend in the same month of year − 1 for the same brand + market (+ channel), whether or not last year had a budget row. Same currency rules (no conversion).
2. GET /api/monthly-budgets/tracker?channel_id= so the tracker can be narrowed to Events (spend from initiatives in that channel).
3. Tests with spend across two years, including a month that had spend last year but no budget.

Front end:
4. Tracker: a Prior year column and per-month change (%); a chart (Recharts, already installed) of budget, spent and prior-year spent by month, using theme colours for dark mode.
5. A channel filter on the tracker; the Events list header gets a "Budget vs last year" link to /budgets?channel=EVENTS.

Checks: back end py_compile/pytest; front end build/lint; compare one month with Jim's reconciliation sheet.
```

---

## Part 11 — Events on mobile (PLAN.md I9, events first)

### 11.1 Mobile events list

```text
Repo: internal_portal_marketing_front

Task: replace the placeholder Initiatives.native.tsx with a real list, starting with events.

Build: shared services/hooks/types (no window/localStorage/import.meta.env — appStorage/getEnv); @/components/native. Segmented control Events / All channels (default Events), search by name or event code, status filter. Rows: name, code, dates, city or platform, budget-used bar, rating. Pull to refresh, infinite scroll (limit/offset), empty and error states. Tapping opens the event screen (register the route now).

Checks: npm run build, npm run lint, Pixel 7 emulator (npm run android).
```

### 11.2 Mobile event screen

```text
Repo: internal_portal_marketing_front

Task: the event screen on mobile — read-only except results (next task).

Build: InitiativeDetail.native.tsx with Overview (brand, market, owner, dates, status, code, location or platform), Costs (budget / spent / remaining with the bar, by category), Results totals, cost ladder, Verdict (rating, reason, latest decision). Reuse pure formatting helpers (move them to shared code if they import web-only modules). Respect safe-area insets.

Checks: npm run build, npm run lint, emulator.
```

### 11.3 Log results from the booth

```text
Repo: internal_portal_marketing_front

Task: update today's counts during an event from a phone.

Build: on the mobile event screen, "Log results" (INITIATIVES_UPDATE) opens big +/- steppers for Booth visitors, Leads and Demos for the current month, showing the month's saved totals. Save via PUT /outcomes/{period_start}; "Saved" confirmation; on error keep what was typed and offer Retry. Big touch targets, one-handed.

Checks: npm run build, npm run lint, emulator: log values, see them on web.
```

---

## After Part 11: the pilot

```text
Not a coding prompt — a checklist.
1. Import last year's events (Part 7) and one real past trade show with complete costs and results.
2. Check its page reproduces the numbers Marketing expects (cost per lead, per demo, per customer, ROI).
3. Enter this year's monthly budgets from Marketing's sheet; compare the tracker with the sheet for one month, and with Jim's reconciliation.
4. Demo to Tom and Robert; log their feedback in the roadmap's question log.
```
