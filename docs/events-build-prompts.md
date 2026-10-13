# Events — build prompts

One prompt = one small task, built and checked before the next. Paste them in order into Claude Code, opened in the repo named in the prompt's first line. Each prompt says what to build, where, the rules, how to test it, and what is **not** part of it.

**Where the code is today (13 Oct 2026):** create / detail / edit / status for initiatives are built and tested (back end `tools/initiative_router.py`, `services/initiative_service.py`, `models/initiative_model.py`, `tests/`; front end `src/pages/Initiatives/*`). Events are initiatives in the `EVENTS` channel (sub-channels `IN_PERSON_EVENTS`, `VIRTUAL_EVENTS`, `WEBINARS`, `SPONSORSHIPS`). Nothing event-specific exists yet.

## Decisions these prompts assume

Open questions are in the roadmap's question log. Until Marketing answers, the prompts use these defaults, chosen so they're cheap to change later:

| Topic | Assumed default | Question |
|---|---|---|
| Channels per initiative | One channel per initiative (as built) | Q38 |
| Event codes | The portal generates them: `EVT-<year>-<0001>`, unique, never reused. The travel system uses ours | Q35 |
| Event types | Optional reference list: Trade show, Conference, Roadshow, Customer event, Partner event, Other | Q35 |
| Regions | Reference table seeded with North America, UK & Europe, Australia & NZ, Middle East, Asia, Other | Q33 |
| Required event fields | In-Person: city + country. Sponsorships: country. Virtual / Webinars: none | Q17 |
| Staff time | No stored rates. A calculator creates a spend line (people × days × day rate) | Q12 |
| Currency | Every amount keeps its own currency. Totals are per currency; mixed currencies are flagged, never converted | Q13 |
| Rating | High = target met; Medium = within 20%; Low = missed by > 20%; Test = inside maturity window or no data | Q08, Q09 |

## Rules that apply to every prompt

Both repos' `CLAUDE.md` already cover most of this; the prompts repeat what matters most:

- **Back end:** raw asyncpg (`pool = get_pool()` then `async with pool.acquire() as conn`), no SQL in loops (use `UNNEST`), new tables go in `postgresql_db/schema_metadata.py` **and** an Alembic autogenerate migration (CI fails on drift), audit with `services/rbac_service.log_audit(conn, ...)` inside the same transaction, permissions with `require_permission("INITIATIVES_VIEW" | "INITIATIVES_UPDATE" ...)`. Map database constraint names to readable messages like `_CONSTRAINT_MESSAGES` in `initiative_service.py`.
- **Tests:** follow `tests/conftest.py` (`client`, `admin`, `viewer`, `ref` fixtures). Any new table that references `initiatives` uses `ON DELETE CASCADE` so the fixtures' cleanup still works.
- **Front end:** API calls only through `src/services/*` + `apiClient`; types in `src/types/*`; React Query hooks in `src/hooks/*` with keys from `src/services/queryKeys.ts`; shadcn components; toasts with `sonner`; gate buttons with `hasPermission("INITIATIVES_UPDATE")`. Follow the existing `useState` + `validate()` form pattern in `InitiativeFormDialog.tsx`.
- **Checks before finishing:** back end `wsl python3 -m py_compile <touched files>`, `pytest tests/ -q`, `git diff --check`. Front end `npm run build` and `npm run lint`. Don't commit; I review and commit each task.

---

## Part 1 — Event details (PLAN.md E1)

### 1.1 Regions and event types reference data

```text
Repo: internal_portal_marketing_back

Task: add two reference lists that event details will use — regions and event types — and serve them with the rest of the reference data.

Context: reference tables live in postgresql_db/schema_metadata.py and were created/seeded in migration 898548e5a993. They are served by services/reference_data_service.py (cached in master_data_cache) through GET /api/reference-data, shaped by models/reference_data_model.py. Follow that exact pattern.

Build:
1. Two tables with the same shape as the other reference tables (id, code unique, name, sort_order, is_active, created_at):
   - regions, seeded: NORTH_AMERICA "North America", UK_EUROPE "UK & Europe", AUSTRALIA_NZ "Australia & NZ", MIDDLE_EAST "Middle East", ASIA "Asia", OTHER "Other".
   - event_types, seeded: TRADE_SHOW "Trade show", CONFERENCE "Conference", ROADSHOW "Roadshow", CUSTOMER_EVENT "Customer event", PARTNER_EVENT "Partner event", OTHER "Other".
2. Add both to schema_metadata.py, then generate one Alembic migration with autogenerate and add the seed inserts to it (same style as 898548e5a993, including downgrade).
3. Add `regions` and `event_types` to the GET /api/reference-data response and its Pydantic model. Keep the cache key/TTL behaviour unchanged.
4. Test: extend or add a test that GET /api/reference-data returns both lists with the seeded codes, and that a viewer without Marketing permissions gets the same access the existing reference-data endpoint gives today.

Not part of this task: event details table, any UI.
Checks: alembic upgrade head then `alembic check` (no drift), py_compile, pytest, git diff --check.
```

### 1.2 Event details table

```text
Repo: internal_portal_marketing_back

Task: create the event_details table — one row per event initiative — with a generated event code. No API yet.

Build:
1. In postgresql_db/schema_metadata.py add table event_details:
   - initiative_id UUID primary key, FK → initiatives.id ON DELETE CASCADE
   - event_code VARCHAR(20) NOT NULL, unique (constraint name event_details_event_code_key)
   - event_type_id INT NULL FK → event_types.id
   - venue VARCHAR(255) NULL, city VARCHAR(120) NULL
   - country_code CHAR(2) NULL with a check that it is two upper-case letters (event_details_country_code_check)
   - region_id INT NULL FK → regions.id
   - created_at / updated_at timestamptz default now(), created_by / updated_by UUID FK → users.id (same as initiatives)
2. Add a sequence event_code_seq used to generate codes. The code format is EVT-<4-digit year>-<4-digit zero-padded number>, e.g. EVT-2026-0007; the year is the year the event row is created. Put the generation in SQL so concurrent creates can't collide (e.g. a column default or an expression used by the insert: 'EVT-' || to_char(now(), 'YYYY') || '-' || lpad(nextval('event_code_seq')::text, 4, '0')). Make sure autogenerate + `alembic check` are happy with however you declare it; add anything autogenerate can't detect by hand in the migration, as was done for initiatives_goal_requires_stage_check.
3. One Alembic migration (autogenerate, then hand-add the sequence if needed) with a working downgrade.
4. In scripts/seed_sample_initiatives.py, give the three sample events event details (a code, a city/country/region for the in-person expo and sponsorship, none for the webinar). Keep the script safe to re-run.

Not part of this task: API changes, UI.
Checks: alembic upgrade head, downgrade -1, upgrade head again, `alembic check`; run the seed script twice; py_compile; git diff --check.
```

### 1.3 Save event details when an event is created

```text
Repo: internal_portal_marketing_back

Task: let POST /api/initiatives accept event details, saved in the same transaction as the initiative.

Build:
1. models/initiative_model.py: add EventDetailsInput (event_type_id, venue, city, country_code, region_id — all optional; strip strings, empty → None, upper-case country_code, validate ^[A-Z]{2}$). Add `event: EventDetailsInput | None = None` to InitiativeCreate. The client never sends event_code.
2. services/initiative_service.py create_initiative:
   - Look up the channel and sub-channel codes once (single query).
   - If the channel is EVENTS: always insert an event_details row (even when `event` is null — every event gets a code), inside the existing transaction, and include the event fields + generated event_code in the audit new_value.
   - If the channel is not EVENTS and `event` was sent → InitiativeValidationError("Event details can only be added to initiatives in the Events channel.").
   - Required fields by sub-channel (keep the rule in one dict constant so it's easy to change — it's an open question with Marketing): IN_PERSON_EVENTS needs city and country_code; SPONSORSHIPS needs country_code; VIRTUAL_EVENTS and WEBINARS need nothing. Message example: "In-person events need a city and a country."
   - Map FK/check violations on event_details to readable messages in _CONSTRAINT_MESSAGES (unknown event type, unknown region, invalid country code).
3. Response: for now return the initiative as today; reading event fields back is the next task.
4. Tests in tests/test_events_create.py:
   - creating an in-person event with details stores a row with a code matching ^EVT-\d{4}-\d{4}$
   - creating an event with no `event` still gets a code
   - two events get different codes
   - in-person without city/country → 422 with the message; webinar without location → 201
   - `event` on a Paid Search initiative → 422
   - unknown region / event type / bad country code → 422
   - audit entry contains event_code

Not part of this task: GET/PATCH changes, front end.
Checks: py_compile, pytest, git diff --check.
```

### 1.4 Return and edit event details

```text
Repo: internal_portal_marketing_back

Task: include event details wherever an initiative is returned, let PATCH change them, and make the list searchable by event code.

Build:
1. services/initiative_service.py `_SELECT`: LEFT JOIN event_details ed, event_types et, regions r. Add columns event_code, event_type_id, event_type_name, venue, city, country_code, region_id, region_name. Add the same nullable fields to InitiativeListItem (models). Non-event initiatives return nulls.
2. List search (`search` param): match name OR event_code (ILIKE), so typing "EVT-2026-0007" finds the event.
3. PATCH /api/initiatives/{id}: add `event: EventDetailsUpdate | None` (partial — only fields sent change; event_code is never editable).
   - Lock and read the current event_details row along with the initiative (FOR UPDATE), apply only changed fields, update updated_at/updated_by, and audit old/new for the event fields in the same audit entry as any initiative changes (one UPDATE audit row per request).
   - Apply the sub-channel required-field rule to the resulting state (after the change), so switching an event from Webinar to In-Person without a city fails with the message.
   - Changing the channel away from EVENTS when event details exist → InitiativeValidationError("Remove this initiative's event details before moving it to another channel.") — don't silently delete them.
   - Changing a non-event initiative INTO Events creates its event_details row (with a code) in the same transaction.
4. Tests (tests/test_events_update.py): GET returns event fields; list search by code; partial event update audits only changed fields; required-field rule after sub-channel change; moving out of Events is blocked; moving into Events creates a code; viewer can't PATCH.

Not part of this task: front end.
Checks: py_compile, pytest (all existing tests must still pass), git diff --check.
```

### 1.5 Event fields in the create / edit form

```text
Repo: internal_portal_marketing_front

Task: show an "Event details" section in InitiativeFormDialog when the chosen channel is Events, and send it to the API.

Build:
1. src/types/initiatives.ts: add the event fields to InitiativeListItem (event_code, event_type_id/name, venue, city, country_code, region_id/name — all nullable) and an `event?` object to InitiativeCreate / InitiativeUpdate. src/types/referenceData.ts: add regions and event_types.
2. src/lib/countries.ts: a static list of ISO 3166-1 alpha-2 countries { code, name }, sorted by name (shared code — no browser APIs).
3. src/pages/Initiatives/InitiativeFormDialog.tsx:
   - When the selected channel's code is EVENTS, render an "Event details" block under the dates: Event type (select, optional), Venue, City, Country (searchable select using countries.ts), Region (select).
   - Mark fields required according to the sub-channel (In-Person: city + country; Sponsorships: country). Put that rule in one exported constant (e.g. src/pages/Initiatives/eventRules.ts) mirroring the back-end rule, and use it in validate() so people get instant feedback.
   - Hide the block (and don't send `event`) for other channels. When editing an event, prefill from the initiative and send only changed event fields in PATCH.
   - Show the event code read-only when editing ("EVT-2026-0007 · generated automatically"); on create show a hint that the code is generated on save.
4. Keep the dialog usable on small screens (the block stacks to one column).

Not part of this task: detail page, list columns.
Checks: npm run build, npm run lint. Manually: create an in-person event (validation blocks missing city), create a webinar with no location, edit an event's city; confirm the PATCH body only contains what changed (network tab).
```

### 1.6 Event details on the detail page and in the list

```text
Repo: internal_portal_marketing_front

Task: show event details on the initiative page and event columns in the list when viewing the Events channel.

Build:
1. src/pages/Initiatives/InitiativeDetail.tsx: for Events initiatives add an "Event" section next to Overview: Event code (with a copy button — navigator.clipboard inside the click handler, fall back silently), Event type, Venue, City, Country (full name via countries.ts), Region. Put the event code next to the status badge in the header too.
2. src/pages/Initiatives/Initiatives.tsx: when the channel filter is Events (the sidebar link uses ?channel=EVENTS), add columns Code and Location ("City, Country" or "Online" for virtual/webinar) and default the sort to upcoming first if the API supports it (if not, leave sorting as is — don't change the API in this task). Update the search placeholder to "Search by name or event code".
3. Format helpers go in src/pages/Initiatives/format.ts.

Not part of this task: costs, results.
Checks: npm run build, npm run lint. Manually check the Events list with the three seeded events, light and dark mode, and a narrow window.
```

---

## Part 2 — Costs (PLAN.md I5, event part of E2)

### 2.1 Spend categories and spend lines table

```text
Repo: internal_portal_marketing_back

Task: add the tables that hold every cost against an initiative. Shared by all channels; events are the first user.

Build:
1. Reference table spend_categories (same shape as other reference tables), seeded: MEDIA_ADS "Media / ads", BOOTH_SPACE "Booth / space", MATERIALS "Materials", STAFF_TIME "Staff time", TRAVEL "Travel", SOFTWARE "Software", OTHER "Other". Add it to GET /api/reference-data.
2. Table spend_lines:
   - id UUID pk (gen_random_uuid()), initiative_id UUID NOT NULL FK → initiatives ON DELETE CASCADE, index on initiative_id
   - category_id INT NOT NULL FK → spend_categories
   - description VARCHAR(255) NULL, note TEXT NULL
   - incurred_on DATE NOT NULL (when the cost happened — used for monthly budget vs actual; booth deposits come months before an event, travel claims after)
   - amount NUMERIC(14,2) NOT NULL, check amount <> 0 (negative allowed for refunds/credits)
   - currency CHAR(3) NOT NULL, check upper-case 3 letters
   - source VARCHAR(30) NOT NULL default 'MANUAL', check in ('MANUAL','TRAVEL_UPLOAD','TRAVEL_FEED','ADS_UPLOAD','STAFF_CALCULATOR')
   - external_ref VARCHAR(100) NULL — the source system's id, for de-duplication; partial unique index on (source, external_ref) where external_ref is not null
   - import_batch_id UUID NULL (FK added later with the imports table — leave a comment)
   - created_at/updated_at/created_by/updated_by like initiatives
3. schema_metadata.py + one autogenerate migration with the seed.

Not part of this task: API, UI.
Checks: upgrade/downgrade/upgrade, alembic check, py_compile, git diff --check.
```

### 2.2 Spend lines API

```text
Repo: internal_portal_marketing_back

Task: list, add, edit and delete spend lines for one initiative.

Build (new files tools/spend_router.py, services/spend_service.py, models/spend_model.py; register the router in server.py like initiative_router):
1. GET  /api/initiatives/{id}/spend-lines            — INITIATIVES_VIEW. Newest incurred_on first, with category name, source, created_by name. 404 if the initiative doesn't exist.
2. POST /api/initiatives/{id}/spend-lines            — INITIATIVES_UPDATE. Body: category_id, incurred_on, amount, currency (default: the initiative's currency), description, note. Source is always MANUAL from this endpoint (STAFF_CALCULATOR allowed too — see 2.6 — but nothing else).
3. PATCH  /api/spend-lines/{line_id}                 — INITIATIVES_UPDATE. Partial update.
4. DELETE /api/spend-lines/{line_id}                 — INITIATIVES_UPDATE. Hard delete (spend lines are not referenced elsewhere).
5. Lines whose source is TRAVEL_UPLOAD / TRAVEL_FEED / ADS_UPLOAD can't be edited or deleted one by one ("This cost came from an import. Undo the import instead.") — imports are undone as a batch later.
6. Audit every create/update/delete with entity_type "SpendLine", entity_id the line id, and include initiative_id in the values; same transaction.
7. Readable messages for constraint errors (unknown category, amount can't be zero, bad currency).
8. Tests (tests/test_spend_lines.py): CRUD happy path; currency defaults to the initiative's; zero amount rejected; viewer can read but not write; imported line can't be edited; line on a missing initiative → 404; deleting the initiative removes its lines.

Not part of this task: totals, UI.
Checks: py_compile, pytest, git diff --check.
```

### 2.3 Budget vs spend totals

```text
Repo: internal_portal_marketing_back

Task: calculate total spend, remaining budget and % used per initiative — on its own endpoint and as a column in the list.

Build:
1. GET /api/initiatives/{id}/spend-summary (INITIATIVES_VIEW) returns:
   - budget_amount, budget_currency
   - totals_by_currency: [{currency, amount}]
   - by_category: [{category_id, category_name, currency, amount}]
   - spent_in_budget_currency (sum of lines in the budget's currency), remaining, percent_used (null when there's no budget)
   - mixed_currencies: true when any line's currency differs from the budget's — the UI will warn that those lines aren't in the % used. Don't convert currencies.
   One or two queries, no loops over lines in Python beyond shaping the result.
2. List endpoint: add spent_amount (in the initiative's currency) and has_mixed_currencies to every row, computed in the same list query (LEFT JOIN LATERAL or a grouped subquery — not one query per row). Add to InitiativeListItem.
3. Tests: totals with several categories; refund (negative) reduces the total; a GBP line on a USD budget sets mixed_currencies and isn't counted in percent_used; no budget → percent_used null; list shows spent_amount.

Not part of this task: UI.
Checks: py_compile, pytest, git diff --check.
```

### 2.4 Costs section on the event page

```text
Repo: internal_portal_marketing_front

Task: replace the "Budget & spend" placeholder on the initiative page with a working costs table.

Build:
1. src/types/spend.ts, src/services/spendService.ts, src/hooks/useSpend.ts (queryKeys: spendLines(id), spendSummary(id)). Mutations invalidate spendLines, spendSummary, the initiative and the initiatives list.
2. In InitiativeDetail.tsx, a full-width "Costs" section: table with Date, Category, Description, Amount (formatted with its own currency), Source (badge: Manual / Travel upload / Staff calculator…), and row actions Edit / Delete for manual lines only (hidden without INITIATIVES_UPDATE). Imported lines show a lock icon with the tooltip "Came from an import".
3. "Add cost" opens SpendLineDialog (new file in src/pages/Initiatives/): category, date (default today), amount, currency (default the initiative's), description, note. Validate amount ≠ 0. Delete asks for confirmation inside the page (an AlertDialog — window.confirm isn't allowed).
4. Empty state: "No costs recorded yet. Add the booth, materials, travel and staff time as they come in."

Not part of this task: summary bar (next task), staff calculator, upload.
Checks: npm run build, npm run lint; manually add/edit/delete a line and see the list's data refresh.
```

### 2.5 Budget vs spend summary and "Spent" column

```text
Repo: internal_portal_marketing_front

Task: show budget vs spend at a glance on the event page and in the list.

Build:
1. On InitiativeDetail, above the costs table, a summary card from GET /spend-summary: Budget, Spent, Remaining, a progress bar for % used (amber at ≥ 90%, red over 100%), and a small breakdown by category (horizontal bars or a simple list with amounts — no chart library needed).
2. If mixed_currencies is true show a warning line: "Some costs are in other currencies (GBP 1,200) and aren't included in % used."
3. Initiatives list: add a "Spent" column showing spent_amount and a thin % bar under it; show "—" with no budget. Mixed currencies → a small warning icon with a tooltip.

Not part of this task: charts by month (that's the summary page later).
Checks: npm run build, npm run lint; check an over-budget and a no-budget initiative in light and dark mode.
```

### 2.6 Staff time calculator

```text
Repo: internal_portal_marketing_front (plus one small back-end check)

Task: make it easy to record staff time without storing anyone's salary.

Build:
1. On the costs section, an "Add staff time" button (INITIATIVES_UPDATE) opens StaffTimeDialog: number of people, number of days, day rate, currency (default the initiative's), date (default the event's end date, else today), optional note.
2. Show the live total ("5 people × 3 days × $400/day = $6,000") and save it as one spend line: category STAFF_TIME, source STAFF_CALCULATOR, description exactly that sentence, amount = the total.
3. Back end: confirm POST /spend-lines accepts source STAFF_CALCULATOR (task 2.2) — if not, allow it there with a test.
4. Staff-time lines are editable/deletable like manual ones.

Not part of this task: staff rosters or stored rates (open question Q12).
Checks: npm run build, npm run lint, back-end pytest if touched.
```

---

## Part 3 — Travel-cost upload (PLAN.md E2)

### 3.1 Import groundwork: batches table and file parser

```text
Repo: internal_portal_marketing_back

Task: build the shared pieces every upload will use (travel costs now; initiatives and paid ads later). No endpoint yet.

Build:
1. requirements.txt: add python-multipart and openpyxl (pin versions like the rest of the file).
2. Table import_batches: id UUID pk, kind VARCHAR(30) check in ('TRAVEL_COSTS','INITIATIVES','PAID_ADS'), filename VARCHAR(255), status VARCHAR(20) check in ('PREVIEW','CONFIRMED','UNDONE','EXPIRED'), total_rows/valid_rows/error_rows/imported_rows INT, staged_rows JSONB (the parsed, validated rows kept between preview and confirm), created_by UUID FK users, created_at, confirmed_at, confirmed_by, undone_at, undone_by. Add the FK spend_lines.import_batch_id → import_batches.id (ON DELETE SET NULL). Update tests/conftest.py _delete_user to remove import batches created by the temporary user.
3. services/import_parsing.py — pure functions, no database:
   - read_table(filename, bytes) → list[dict[str, str]] for .csv (utf-8 with or without BOM) and .xlsx (first sheet, values not formulas); header names normalised (trim, case-insensitive, spaces/underscores equivalent); skip fully blank rows; raise ImportFileError with a readable message for other types, empty files, or > 5,000 rows.
   - parse_amount(text) → Decimal | None: handles "$1,200.50", "£300", "(150.00)" as negative, "1 200", "." / "-" / "" as blank, Excel errors (#N/A, #DIV/0!, #VALUE!) as invalid.
   - parse_date(text): ISO, DD/MM/YYYY, and Excel date serials/datetimes. Ambiguous formats: DD/MM (UK/AU usage) — document this.
   - parse_currency(text, default) → 3-letter code; maps $→USD, £→GBP, €→EUR, A$→AUD, C$→CAD.
4. Tests tests/test_import_parsing.py (no database needed, so they always run): every parse case above, CSV with BOM, xlsx built in-memory with openpyxl, too many rows, wrong file type.

Not part of this task: endpoints, UI.
Checks: pip install -r requirements.txt, alembic check, py_compile, pytest, git diff --check.
```

### 3.2 Travel-cost upload: preview

```text
Repo: internal_portal_marketing_back

Task: upload a travel-cost file and get back a row-by-row preview. Nothing is saved to spend_lines yet.

File format (document it in the endpoint docstring): columns Event Code (required), Date (required), Amount (required), Currency (optional, default the event's currency), Category (optional, default Travel; accepts category names or codes), Description (optional), Reference (optional but recommended — the travel system's transaction id, used to skip duplicates).

Build (tools/import_router.py, services/travel_import_service.py, models/import_model.py):
1. POST /api/imports/travel-costs (multipart `file`, max 5 MB) — permission INITIATIVES_UPDATE.
2. Parse with services/import_parsing.py. Then, in a constant number of queries (not one per row): look up all event codes in the file, existing (source, external_ref) pairs, and categories.
3. Classify each row: READY, UNKNOWN_EVENT ("No event with code EVT-2026-0099"), DUPLICATE ("Already imported (reference TR-1182)") — also duplicates within the same file, INVALID (list every problem in the row: missing date, bad amount…). Keep the original row number.
4. Save an import_batches row (kind TRAVEL_COSTS, status PREVIEW, counts, staged_rows = the READY rows already normalised: initiative_id, category_id, incurred_on, amount, currency, description, external_ref).
5. Return {batch_id, filename, counts, rows: [{row_number, status, messages, event_code, event_name, date, amount, currency, category, description, reference}]}.
6. Tests: good file → all READY; unknown code; duplicate against an existing line and within the file; invalid amount/date; non-event code; wrong file type → 400 with message; viewer → 403.

Not part of this task: confirm/undo, template, UI.
Checks: py_compile, pytest, git diff --check.
```

### 3.3 Travel-cost upload: confirm, undo and template

```text
Repo: internal_portal_marketing_back

Task: finish the travel-cost import: save a previewed batch, undo a whole batch, and download a template.

Build:
1. POST /api/imports/{batch_id}/confirm (INITIATIVES_UPDATE): only for status PREVIEW and created by the same user (else 409 "This upload was already confirmed" / 403). Insert all staged rows into spend_lines in ONE statement (INSERT … SELECT FROM unnest(...) ) with source TRAVEL_UPLOAD and import_batch_id, ON CONFLICT on the (source, external_ref) unique index DO NOTHING (rows imported by someone else in the meantime are skipped, and counted). Set status CONFIRMED, imported_rows, confirmed_at/by. One audit row: action IMPORT, entity_type "ImportBatch", new_value {kind, filename, imported_rows, skipped}.
2. POST /api/imports/{batch_id}/undo (INITIATIVES_UPDATE): only CONFIRMED batches; delete its spend lines in one statement; status UNDONE; audit action UNDO_IMPORT.
3. Previews older than 24 hours can't be confirmed (status EXPIRED on attempt, message "This preview has expired. Upload the file again.").
4. GET /api/imports/travel-costs/template — an .xlsx with the headers, one example row, a second sheet listing valid categories, and a data-validation dropdown on Category. Built with openpyxl, streamed with a proper filename and content type.
5. GET /api/imports?kind=TRAVEL_COSTS — recent batches (who, when, file, counts, status) for an upload history list.
6. Tests: confirm writes the lines and audits; confirming twice → 409; undo removes exactly that batch's lines; expired preview; template downloads with the right headers; another user can't confirm my preview.

Not part of this task: UI.
Checks: py_compile, pytest, git diff --check.
```

### 3.4 Travel-cost upload screen

```text
Repo: internal_portal_marketing_front

Task: a screen to upload travel costs, check the preview and confirm.

Build:
1. src/types/imports.ts, src/services/importsService.ts (multipart via apiClient.post with FormData; template via apiClient.downloadFile), src/hooks/useImports.ts.
2. Page src/pages/Imports/TravelCostUpload.tsx at route /initiatives/imports/travel-costs, protected with navigationCode INITIATIVES and action CREATE/UPDATE as ProtectedRoute supports. Link to it from the Events list header ("Upload travel costs") and from each event's Costs section.
3. Steps on one page:
   a. Pick a file (drag-and-drop area + file input; .csv/.xlsx only, 5 MB) and a "Download template" link.
   b. Preview: counts as chips (Ready 42 · Unknown event 3 · Duplicate 1 · Invalid 2), filter by status, a table (row #, status badge, event code + name, date, amount, category, description, messages). Problem rows highlighted.
   c. "Import 42 costs" (disabled when 0 ready) / "Cancel". After confirm: toast "Imported 42 travel costs" with an "Undo" action that calls undo.
4. Below: recent uploads (from GET /api/imports?kind=TRAVEL_COSTS) with an Undo button on confirmed ones (confirmation inside the page).
5. Invalidate spend queries for affected initiatives (or all spend queries) after confirm/undo.

Not part of this task: mobile.
Checks: npm run build, npm run lint; manually upload the template with a mix of good and bad rows.
```

---

## Part 4 — Last year's events (history and the pilot)

### 4.1 Event spreadsheet import: back end

```text
Repo: internal_portal_marketing_back

Task: import a spreadsheet of events (Marketing's existing Excel), creating initiatives with event details and, optionally, one total cost line per category. This loads last year's events for prior-year comparisons and gives us a real pilot event.

Reuse services/import_parsing.py and import_batches (kind INITIATIVES). Columns: Name*, Brand*, Sub-channel* (In-Person Events / Virtual Events / Webinars / Sponsorships), Start Date, End Date, Budget, Currency, Status, Owner Email, Event Type, Venue, City, Country, Region, and optional cost columns Booth/Space, Materials, Travel, Staff Time, Other (each becomes one spend line dated the event's end date, source MANUAL, description "Imported total").

Build:
1. POST /api/imports/events (preview), using the same READY / INVALID / DUPLICATE classification: duplicate = same name + brand + start date as an existing initiative or another row. Validate against reference data by name or code (case-insensitive), the sub-channel required-field rule, dates, owner email (unknown → owner is the uploader, with a warning, not an error).
2. Confirm (reuse POST /api/imports/{batch_id}/confirm, dispatching on kind): create initiatives, event_details (codes generated) and spend lines with set-based inserts in one transaction; one audit row for the batch. Undo deletes the initiatives created by the batch (cascades remove their details and lines) — add import_batch_id to initiatives for this.
3. GET /api/imports/events/template with dropdowns for brand, sub-channel, event type, region, status.
4. Tests: good file; invalid sub-channel; missing city for in-person; duplicate; cost columns create lines; undo removes everything.

Not part of this task: UI (next), non-event channels (later — keep the code channel-agnostic where it's free).
Checks: alembic check, py_compile, pytest, git diff --check.
```

### 4.2 Event spreadsheet import: screen

```text
Repo: internal_portal_marketing_front

Task: the upload screen for events, reusing the travel-cost upload components.

Build:
1. Extract the reusable parts of TravelCostUpload (file drop, counts chips, preview table shell, confirm bar, recent uploads list) into src/components/imports/ and use them for both screens — without changing the travel screen's behaviour.
2. Page src/pages/Imports/EventUpload.tsx at /initiatives/imports/events, linked from the Events list ("Import events"). Preview columns: row #, status, name, brand, sub-channel, dates, location, budget, cost totals, messages.
3. After confirm, toast "Imported 18 events" with Undo, and a link to the Events list filtered to Events.

Checks: npm run build, npm run lint; import a file with last year's events and check they appear with codes and costs.
```

---

## Part 5 — Results (PLAN.md I6)

### 5.1 Outcome metrics and outcomes table

```text
Repo: internal_portal_marketing_back

Task: tables for results (funnel metrics) per initiative per period.

Build:
1. Reference table outcome_metrics (reference shape plus `unit` VARCHAR(10) check in ('COUNT','MONEY')), seeded in funnel order: IMPRESSIONS, CLICKS, VISITORS, ENGAGEMENTS, LEADS, FOLLOW_UPS, MQLS, SQLS, TRIALS, DEMOS, CUSTOMERS (COUNT); PIPELINE, REVENUE (MONEY). Add to GET /api/reference-data.
2. Table outcomes: id UUID pk, initiative_id FK ON DELETE CASCADE, period_start DATE NOT NULL (always the 1st of a month — check constraint), metric_id FK, value NUMERIC(16,2) NOT NULL check >= 0, currency CHAR(3) NULL (required when the metric unit is MONEY — enforce in the service), source VARCHAR(30) NOT NULL default 'MANUAL' (MANUAL, UPLOAD, HUBSPOT, GOOGLE_ADS, … — no check list, it'll grow), recorded_at/recorded_by, unique (initiative_id, period_start, metric_id, source). Manual and synced values sit side by side; neither overwrites the other.
3. schema_metadata.py + one migration.

Checks: alembic check, py_compile, git diff --check.
```

### 5.2 Results API

```text
Repo: internal_portal_marketing_back

Task: read and save results for an initiative, one month at a time.

Build (tools/outcome_router.py, services/outcome_service.py, models/outcome_model.py):
1. GET /api/initiatives/{id}/outcomes (INITIATIVES_VIEW): {periods: ["2026-11-01", …], values: [{period_start, metric_code, value, currency, source, recorded_at, recorded_by_name}]}.
2. PUT /api/initiatives/{id}/outcomes/{period_start} (INITIATIVES_UPDATE), body {values: [{metric_code, value | null, currency?}]}: upserts the MANUAL values for that month in ONE statement (INSERT … SELECT FROM unnest … ON CONFLICT DO UPDATE); null deletes that metric's manual value (one DELETE … WHERE metric_id = ANY(...)). period_start must be the 1st of a month (else 422). MONEY metrics default to the initiative's currency.
3. Audit one row per save: entity_type "Outcomes", entity_id the initiative id, old/new values keyed by metric code, with the period.
4. Tests: save and read back; update changes only what was sent; null deletes; not-first-of-month → 422; unknown metric → 422; viewer can't save; a HUBSPOT-sourced value inserted directly in SQL is untouched by a manual save.

Checks: py_compile, pytest, git diff --check.
```

### 5.3 Performance calculation

```text
Repo: internal_portal_marketing_back

Task: turn costs + results into the cost-per-outcome ladder and ROI. Calculated on request, never stored.

Build:
1. services/initiative_metrics.py — pure functions, fully unit-tested, no database:
   - Inputs: total cost (in the initiative's currency), counts per metric (all periods, all sources — when a metric has both MANUAL and a synced source for the same period, prefer the synced one and report that), revenue/pipeline.
   - Outputs: cost_per for visitor, click, lead, follow-up, MQL, SQL, trial, demo, customer; CTR = clicks / impressions; CPM; conversion rates between consecutive ladder steps present (e.g. lead → demo, demo → customer); ROI = (revenue − cost) / cost. Any division by zero or missing input → None, never an error.
2. GET /api/initiatives/{id}/performance (INITIATIVES_VIEW) assembling inputs with a fixed number of queries; costs in other currencies are excluded and reported (has_mixed_currencies, excluded_by_currency).
3. Unit tests including the CEO's example: cost 20,000, leads 200, demos 20, customers 2, revenue 20,000 → CPL 100, cost per demo 1,000, CAC 10,000, ROI 0.0. Plus zero leads, no cost, revenue without customers.
4. One API test that the endpoint returns the ladder for a seeded event.

Checks: py_compile, pytest, git diff --check.
```

### 5.4 Results section on the event page

```text
Repo: internal_portal_marketing_front

Task: enter and view results on the initiative page.

Build:
1. src/types/outcomes.ts, src/services/outcomesService.ts, src/hooks/useOutcomes.ts (keys outcomes(id), performance(id); saving invalidates both).
2. Replace the "Results" placeholder with a Results section: a month picker (defaults to the event's start month; months with data shown as chips), and a form with the metrics that matter for events, in funnel order: Booth visitors, Leads, Follow-ups, Demos, MQLs, SQLs, Customers, Pipeline, Revenue (money in the initiative's currency). Keep the list of "metrics shown per channel" in one constant so other channels can reuse the section later.
3. Save sends only changed values; clearing a field sends null. Synced values (source ≠ MANUAL) show read-only with their source.
4. Under the form, a totals row across all months.

Checks: npm run build, npm run lint; enter November and December results for the seeded expo and see totals add up.
```

### 5.5 Cost ladder

```text
Repo: internal_portal_marketing_front

Task: show what each outcome cost, the way the CEO thinks about it.

Build:
1. A "Cost per outcome" card from GET /performance: a vertical ladder — Visitor $X → Lead $100 → Demo $1,000 → Customer $10,000 — with the count beside each step and the conversion rate between steps on the connector ("10% of leads → demo"). Steps without data show "—".
2. Revenue, total cost and ROI underneath. ROI ≤ 0 is shown in the warning colour with "Broke even or lost money".
3. If costs in other currencies were excluded, say so under the card.
4. Pure formatting helpers in src/pages/Initiatives/format.ts with no React.

Checks: npm run build, npm run lint; the seeded expo with the CEO's numbers must show $100 / $1,000 / $10,000 and ROI 0%.
```

---

## Part 6 — "Was it worth it?" (PLAN.md I7)

### 6.1 Evaluation settings and defaults by sub-channel

```text
Repo: internal_portal_marketing_back

Task: store what success means for each initiative, pre-filled from defaults per sub-channel.

Build:
1. Table evaluation_defaults: sub_channel_id pk FK, funnel_stage_id, goal_id, primary_kpi_id, maturity_days INT. Seed for events (proposals pending Marketing — Q36): In-Person Events → Conversion / Demos / CP-SQL, 180 days; Sponsorships → Consideration / Leads / CPL, 180; Virtual Events → Consideration / Leads / CPL, 90; Webinars → Consideration / Leads / CPL, 90. Expose them in GET /api/reference-data as evaluation_defaults.
2. Table initiative_evaluations: initiative_id pk FK ON DELETE CASCADE, target_value NUMERIC(14,2) NULL, maturity_days INT NOT NULL default 180 check 0–730, revenue_basis VARCHAR(30) NOT NULL default 'CLOSED_REVENUE' (check: only that value for now), updated_at/updated_by.
3. GET /api/initiatives/{id}/evaluation (VIEW) — returns the row or the defaults for its sub-channel when none exists; PUT (UPDATE) upserts and audits (entity "Evaluation").
4. On initiative create (services/initiative_service.py): if funnel stage/goal/KPI weren't sent, fill them from evaluation_defaults for the sub-channel. Test this.
5. Tests: defaults returned when no row; upsert; audit; viewer can't PUT; create fills defaults.

Checks: alembic check, py_compile, pytest, git diff --check.
```

### 6.2 Suggested rating

```text
Repo: internal_portal_marketing_back

Task: suggest High / Medium / Low / Test by comparing the primary KPI with its target.

Build:
1. In services/initiative_metrics.py add:
   - KPI_DEFINITIONS: for each primary KPI code, how to read its actual value from the performance result and whether lower or higher is better (CPL/CP-* /CAC/CPC/CPM lower; CTR, ROI, Revenue, Pipeline Generated, Impressions higher). CONVERSION_RATE and LTV_CAC: unsupported for now → rating Test with reason "Not calculated yet".
   - suggest_rating(kpi_code, actual, target, event_end_date, maturity_days, today) → {rating, reason}:
     Test when today < end_date + maturity_days ("23 days left in the 180-day window"), or no target, or no actual;
     otherwise High if the target is met or beaten, Medium if within 20% of it, Low if missed by more than 20%. Keep 20% as a named constant.
2. Add {primary_kpi, actual, target, rating, reason, matures_on} to GET /performance.
3. Unit tests: lower-is-better and higher-is-better at exactly the target, 19.9%, 20%, 20.1% off; inside the window; no target; unsupported KPI.

Checks: py_compile, pytest, git diff --check.
```

### 6.3 Scale-or-cut decision

```text
Repo: internal_portal_marketing_back

Task: record Marketing's decision for an initiative, keeping the history.

Build:
1. Table initiative_decisions: id UUID pk, initiative_id FK ON DELETE CASCADE, scale_or_cut_id FK → scale_or_cut_options, suggested_rating VARCHAR(10) (what the system suggested at the time), note TEXT, decided_by FK users, decided_at timestamptz default now().
2. POST /api/initiatives/{id}/decisions (UPDATE) — body {scale_or_cut_id, note}; store the current suggested rating with it; audit (entity "Decision"). GET /api/initiatives/{id}/decisions (VIEW) — newest first with names.
3. Add latest_decision (name, decided_at, decided_by_name) to GET /performance and to the list rows (one LATERAL join, not per-row queries).
4. Tests: record and list; latest shows on the list; viewer can't decide.

Checks: alembic check, py_compile, pytest, git diff --check.
```

### 6.4 "Was it worth it?" section

```text
Repo: internal_portal_marketing_front

Task: replace the "Was it worth it?" placeholder with the verdict.

Build:
1. Evaluation settings row (editable with UPDATE permission): primary KPI (read from the initiative), target (in the KPI's unit — money with currency for cost KPIs, % for CTR/ROI), maturity window (days). Save via PUT /evaluation.
2. Verdict: big rating badge (High green / Medium amber / Low red / Test grey) with the reason sentence, actual vs target ("CP-SQL $1,250 vs target $1,000 — missed by 25%"), and "Rating is final on 2 May 2027" while inside the window.
3. Decision: a select of scale-or-cut options with their meanings, the system's suggestion pre-selected (High → Scale, Medium → Optimize, Low → Pause, Test → Test More), a note field, "Record decision". Below, the decision history.
4. Lay the page out so the order reads Overview → Event → Costs → Results → Cost per outcome → Was it worth it?

Checks: npm run build, npm run lint; check an event inside the window, one past it that met target, and one that missed.
```

### 6.5 Defaults in the form, rating in the list

```text
Repo: internal_portal_marketing_front (+ back end for the filter)

Task: pre-fill goals from the sub-channel and show ratings in the Events list.

Build:
1. InitiativeFormDialog: when a sub-channel with evaluation_defaults is chosen on CREATE and the funnel stage/goal/KPI are still empty, pre-fill them and show "Filled in from In-Person Events defaults — change if needed". Never overwrite values the person already chose.
2. Back end: add suggested rating to list rows without per-row queries — compute costs and outcome totals for the page's initiative ids with two grouped queries, then call suggest_rating in Python for the page (≤ 200 rows). Add a `rating` filter (applied after calculation is fine for now since pages are small — note the limitation in a comment). Tests.
3. Front end list: Rating column (badge) and Decision column (latest decision name), plus a rating filter next to status.

Checks: back end py_compile/pytest; front end build/lint.
```

---

## Part 7 — Events summary (PLAN.md I8, events first)

### 7.1 Events rollup API

```text
Repo: internal_portal_marketing_back

Task: budget vs actual vs prior year for events, by brand, region and month.

Build (tools/rollup_router.py, services/rollup_service.py, models/rollup_model.py):
1. GET /api/rollups/events?year=2026&brand_id=&region_id=&currency=USD (INITIATIVES_VIEW).
2. Rules (document in the docstring):
   - Actual = spend lines grouped by month of incurred_on (matches Marketing's monthly reconciliation).
   - Budget = each event's budget, placed in the month of its start date (budgets aren't phased by month yet).
   - Prior year = the same months of year − 1, same filters.
   - Only amounts in the requested currency are included; return excluded totals per other currency so the page can say so. No FX conversion.
3. Response: by_month [{month, budget, actual, prior_year_actual, variance}], by_brand and by_region [{id, name, events, budget, actual, prior_year_actual, leads, cost_per_lead}], totals, excluded_currencies. Use a small fixed number of grouped queries.
4. Tests with a handful of events across two years and two brands, including a GBP line excluded from a USD rollup.

Checks: py_compile, pytest, git diff --check.
```

### 7.2 Events summary page

```text
Repo: internal_portal_marketing_front

Task: one page that answers "how are events doing against budget this year, and versus last year?"

Build:
1. Route /initiatives/summary?channel=EVENTS, linked from the Events list header ("Summary"). Filters: year (default current), brand, region, currency (default USD).
2. Top: four figures — Budget, Actual, Variance (red when over), Prior year actual (with % change).
3. Chart (Recharts — already installed): by month, bars for budget and actual and a line for prior year. Use theme tokens for colours so it works in dark mode; label axes with currency.
4. Tables: by brand and by region (events, budget, actual, prior year, leads, cost per lead). Rows link to the Events list filtered accordingly.
5. If excluded_currencies is not empty, a note: "GBP 12,400 not included — switch currency to GBP to see it."

Checks: npm run build, npm run lint; review with last year's imported events.
```

---

## Part 8 — Events on mobile (PLAN.md I9, events first)

### 8.1 Mobile events list

```text
Repo: internal_portal_marketing_front

Task: replace the placeholder Initiatives.native.tsx with a real list, starting with events.

Build:
1. Use the same services/hooks/types as web (shared code must not use window/localStorage/import.meta.env — use appStorage/getEnv). Components from @/components/native.
2. Screen: segmented control Events / All channels (default Events), search by name or event code, a status filter. Each row: name, code, dates, city, a small budget-used bar, rating badge.
3. Pull to refresh; infinite scroll using limit/offset; empty and error states.
4. Tapping a row navigates to the event detail screen (next task; register the route now with a placeholder).

Checks: npm run build, npm run lint, run on the Pixel 7 emulator (npm run android) with the seeded data.
```

### 8.2 Mobile event detail

```text
Repo: internal_portal_marketing_front

Task: the event screen on mobile — read-only except results (next task).

Build: InitiativeDetail.native.tsx with sections Overview (brand, owner, dates, status, code, location), Costs (budget / spent / remaining with the bar, by category), Results totals, Cost per outcome ladder, Verdict (rating, reason, latest decision). Reuse the pure formatting helpers from web (move them to shared code if they import web-only modules). Respect the safe-area insets.

Checks: npm run build, npm run lint, emulator.
```

### 8.3 Log results from the booth

```text
Repo: internal_portal_marketing_front

Task: let staff update today's counts during an event from their phone.

Build:
1. On the mobile event screen, a "Log results" button (INITIATIVES_UPDATE) opening a screen with large +/- steppers for Booth visitors, Leads and Demos for the current month, showing the saved total for the month.
2. Save sends the new totals via PUT /outcomes/{period_start}. Show "Saved" and handle offline/errors with a clear message and a retry button — don't lose what was typed.
3. Big touch targets, works one-handed.

Checks: npm run build, npm run lint, emulator: log values, check they appear on web.
```

---

## After Part 8: the pilot

```text
Not a coding prompt — a checklist.
1. Import last year's events (Part 4) and one real past trade show with complete costs and results.
2. Check its page reproduces the numbers Marketing expects (cost per lead, per demo, per customer, ROI).
3. Run the Events summary for last year and this year; compare with Jim's monthly reconciliation spreadsheet for one month.
4. Demo to Tom and Robert; log their feedback as questions in the roadmap's question log.
```
