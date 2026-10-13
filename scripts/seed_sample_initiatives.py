#!/usr/bin/env python3
"""Seed sample marketing initiatives across several channels — LOCAL DEVELOPMENT ONLY.

Usage:
    python scripts/seed_sample_initiatives.py

Refuses to run unless DATABASE_URL points at localhost. Safe to re-run: initiatives
whose name already exists are skipped. Owner is the first super admin user.
"""
import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from postgresql_db.database import close_pool, create_pool, get_pool

# (name, brand, channel, sub-channel, start, end, budget, currency, status)
SAMPLES = [
    ("London - Wfinder - LP1", "OTHERSHIP", "PAID_SEARCH", "GOOGLE_ADS", date(2026, 8, 3), date(2026, 8, 31), 4850, "GBP", "COMPLETED"),
    ("NA - Desk Booking - Main LP", "DESKFLEX", "PAID_SEARCH", "GOOGLE_ADS", date(2026, 9, 1), date(2026, 10, 1), 9000, "USD", "ACTIVE"),
    ("NA - Desk Booking - Main LP (Bing)", "DESKFLEX", "PAID_SEARCH", "BING_ADS", date(2026, 9, 21), None, 3000, "USD", "ACTIVE"),
    ("AU - Safety Mgm Software - Main LP", "WORKAWARE", "PAID_SEARCH", "GOOGLE_ADS", date(2026, 9, 19), None, 7800, "AUD", "ACTIVE"),
    ("Facilities Managers - LinkedIn Lead Gen", "DESKFLEX", "PAID_SOCIAL", "LINKEDIN_ADS", date(2026, 10, 1), date(2026, 12, 31), 6000, "USD", "PLANNED"),
    ("Hybrid Work Retargeting", "OTHERSHIP", "PAID_SOCIAL", "META_ADS", date(2026, 7, 1), date(2026, 8, 15), 2500, "GBP", "PAUSED"),
    ("Workplace Analytics Blog Series", "TBD_ALL", "SEO", "BLOG", date(2026, 6, 1), None, 1200, "USD", "ACTIVE"),
    ("Trial-to-Paid Nurture", "TILLERSTACK", "EMAIL_MARKETING", "EMAIL_NURTURE", date(2026, 9, 1), None, 500, "USD", "ACTIVE"),
    ("Facility Expo 2026 - Booth", "DESKFLEX", "EVENTS", "IN_PERSON_EVENTS", date(2026, 11, 4), date(2026, 11, 6), 20000, "USD", "PLANNED"),
    ("Safety Compliance Webinar", "WORKAWARE", "EVENTS", "WEBINARS", date(2026, 10, 22), date(2026, 10, 22), 800, "AUD", "PLANNED"),
    ("Drone Industry Summit Sponsorship", "K9SKY", "EVENTS", "SPONSORSHIPS", date(2026, 5, 12), date(2026, 5, 14), 15000, "USD", "COMPLETED"),
    ("Reseller Launch - EMEA", "ZOO_OFFICE", "PARTNERSHIPS", "RESELLERS", date(2026, 10, 15), None, 4000, "GBP", "PLANNED"),
    ("Creator Program Pilot", "PACE_PLUS", "INFLUENCER_MARKETING", "COMMUNITY", date(2026, 8, 1), date(2026, 9, 30), 3500, "USD", "CANCELLED"),
]


def _require_local_database() -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not any(host in url for host in ("localhost", "127.0.0.1", "host.docker.internal")):
        sys.exit("Refusing to seed sample data: DATABASE_URL is not a local database.")


async def seed() -> None:
    await create_pool()
    _require_local_database()
    try:
        async with get_pool().acquire() as conn:
            owner_id = await conn.fetchval(
                "SELECT id FROM users WHERE is_super_admin AND is_active ORDER BY created_at LIMIT 1"
            )
            columns = list(zip(*SAMPLES))
            inserted = await conn.fetch(
                """
                INSERT INTO initiatives (name, brand_id, channel_id, sub_channel_id, owner_user_id,
                                         start_date, end_date, budget_amount, currency, status,
                                         created_by, updated_by)
                SELECT s.name, b.id, c.id, sc.id, $10, s.start_date, s.end_date, s.budget, s.currency, s.status,
                       $10, $10
                FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::date[], $6::date[],
                            $7::numeric[], $8::text[], $9::text[])
                     AS s(name, brand, channel, sub_channel, start_date, end_date, budget, currency, status)
                JOIN brands b ON b.code = s.brand
                JOIN channels c ON c.code = s.channel
                JOIN sub_channels sc ON sc.code = s.sub_channel
                WHERE NOT EXISTS (SELECT 1 FROM initiatives i WHERE i.name = s.name)
                RETURNING name
                """,
                *[list(col) for col in columns],
                owner_id,
            )
        print(f"Inserted {len(inserted)} sample initiative(s); {len(SAMPLES) - len(inserted)} already existed.")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(seed())
