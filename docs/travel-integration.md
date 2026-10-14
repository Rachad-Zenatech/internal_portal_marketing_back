# Marketing Portal ↔ Travel Management System

For the travel management system's developers. The Marketing Portal decides **which events happen and who goes**;
the travel system **books and pays for their travel** (flights, hotels, Ubers and ground transport, stipends, cargo).
Nothing about bookings or receipts is stored in the Marketing Portal.

## How to call us

Service-to-service, with a token from `generate_scoped_outbound_token` (the shared portal template), signed with the
shared secret:

- `service`: `travel-management`
- Scopes this portal grants you: `events:read`, `events:travel_status_write`
- Header: `Authorization: Bearer <token>`

People's sign-ins are refused on these routes (apart from super admins).

## 1. Read events and the staff who need travel

`GET /api/integrations/travel/events?updated_since=2026-10-14T00:00:00Z`

- Returns events that are current, upcoming, or ended in the last 90 days. `updated_since` (optional) returns only events
  whose details or staff changed after that time — poll with the time of your last successful read.
- **Match on `event_code`** (e.g. `EVT-2026-0007`). Please store it on every booking and receipt, so their costs can
  come back to the right event later.
- `staff` lists only people who need travel. **Someone removed from an event no longer appears** — treat that as
  "cancel their bookings". A cancelled event keeps appearing with `status: "CANCELLED"`.

```json
[
  {
    "event_code": "EVT-2026-0007",
    "initiative_id": "6f1c…",
    "name": "Facility Expo 2026 - Booth",
    "status": "PLANNED",
    "brand": "DeskFlex",
    "sub_channel": "In-Person Events",
    "start_date": "2026-11-04",
    "end_date": "2026-11-06",
    "venue": "Las Vegas Convention Center",
    "city": "Las Vegas",
    "country_code": "US",
    "platform": null,
    "owner_name": "Priya Rampat",
    "owner_email": "…@zenatech.com",
    "updated_at": "2026-10-14T15:02:11Z",
    "staff": [
      {
        "assignment_id": "a2b9…",
        "user_id": "…",
        "full_name": "…",
        "email": "…@zenatech.com",
        "role": "BOOTH_STAFF",
        "departs_from": "Toronto (YYZ)",
        "arrive_on": "2026-11-03",
        "depart_on": "2026-11-06",
        "needs_flight": true,
        "needs_hotel": true,
        "needs_ground_transport": true,
        "needs_stipend": true,
        "cargo_note": "2 demo drones in Pelican cases",
        "notes": null,
        "readiness": "READY",
        "missing": [],
        "updated_at": "2026-10-14T15:02:11Z"
      }
    ]
  }
]
```

- `readiness` is `READY` when you have what you need to book, `MISSING_DETAILS` otherwise (`missing` says what, e.g.
  `["where they fly from"]`), or your own status once you've reported one (below). Roles: `LEAD`, `BOOTH_STAFF`, `DEMO`,
  `SPEAKER`, `SETUP`, `EXECUTIVE`, `OTHER`.

## 2. Tell us how booking is going (optional, but it shows on Gigi's and Priya's screen)

`POST /api/integrations/travel/staff-status` with `{"assignment_id": "a2b9…", "travel_status": "BOOKED"}`

- `travel_status`: `REQUESTED` (you have it), `BOOKED`, or `CANCELLED`. `204` on success, `404` for an unknown assignment.

## 3. Later: travel costs back into the event

Not built yet. Proposal: you send approved and reimbursed costs per `event_code` (flight, hotel, ground transport,
per diem, cargo, other) with your transaction id, so they appear in the event's costs as "from the travel system".
To agree together.

## Questions for you

1. Can every booking and receipt carry our `event_code`?
2. Do you want to keep your own event list, or read ours (above) and drop yours?
3. Is polling `GET /events` (e.g. every 15 minutes) fine, or do you want us to push changes?
4. What else do you need per person (passport name, frequent-flyer numbers…)? Those are personal details — better kept
   in your system than sent from ours.
