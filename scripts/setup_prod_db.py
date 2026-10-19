"""Set up or update the production database schema, using the PROD_* values in .env.

    python scripts/setup_prod_db.py --check   # read-only: what's there and how it differs from local
    python scripts/setup_prod_db.py           # backup, then setup scripts + alembic upgrade head, after you confirm

.env's own DATABASE_URL stays on the local database; the prod URL is passed to each step for that run only.
Never seeds sample data. Safe to re-run.
"""
import argparse
import asyncio
import os
import shutil
import ssl
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STEPS = [
    [sys.executable, "-m", "postgresql_db.setup_pbac_schema"],
    [sys.executable, "-m", "postgresql_db.setup_graph_sync_schema"],
    [sys.executable, "-m", "postgresql_db.notifications_schema"],
    [sys.executable, "-m", "alembic", "upgrade", "head"],
]
# --sample-data: the local mock data. Demo events and campaigns can be taken out again with their "remove" command;
# the sample initiatives can't
SAMPLE_STEPS = [
    [sys.executable, "scripts/seed_sample_initiatives.py"],
    [sys.executable, "scripts/demo_events.py", "add"],
    [sys.executable, "scripts/demo_ads.py", "add"],
]
# Schema changes give up after this long on a busy table, instead of making the live app's queries wait
LOCK_TIMEOUT_MS = "10000"
BACKUP_DIR = Path.home() / "marketing_db_backups"

# Migration versions written by other copies of the template, and this repo's version with the same marketing schema
FOREIGN_VERSIONS = {"eac2b3835a06": "e8c4519967d0"}
# Template columns this app writes to that those copies don't have
MISSING_COLUMNS = {
    "notifications": {"sender_name": "VARCHAR(255)", "sender_avatar": "VARCHAR(500)", "attachments": "JSONB"},
}
# Tables this app inserts into: their extra required columns from those copies are made optional
INSERTED_TABLES = ["users", "roles", "notifications", "login_activity_logs", "role_navigation_permissions"]


def prod_settings() -> dict[str, str]:
    env = dotenv_values(os.path.join(ROOT, ".env"))
    url = env.get("PROD_DATABASE_URL") or ""
    if not url:
        sys.exit("PROD_DATABASE_URL is not set in .env.")
    settings = {"DATABASE_URL": url, "DATABASE_SSL": env.get("PROD_DATABASE_SSL") or "true", "DB_LOCK_TIMEOUT": LOCK_TIMEOUT_MS}
    if env.get("PROD_INITIAL_SUPER_ADMIN_EMAILS"):
        settings["INITIAL_SUPER_ADMIN_EMAILS"] = env["PROD_INITIAL_SUPER_ADMIN_EMAILS"]
    return settings


def local_database_url() -> str | None:
    """The local database, for comparing; .env's DATABASE_URL may point at production"""
    env = dotenv_values(os.path.join(ROOT, ".env"))
    url = env.get("LOCAL_DATABASE_URL") or env.get("DATABASE_URL")
    return url if url and urlsplit(url).hostname in ("localhost", "127.0.0.1", "host.docker.internal") else None


async def connect(settings: dict[str, str]) -> asyncpg.Connection:
    ctx = None
    if settings["DATABASE_SSL"].lower() in ("true", "require", "1"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return await asyncpg.connect(settings["DATABASE_URL"], ssl=ctx, timeout=20)


def in_repo(version: str) -> bool:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "history", "-r", f"{version}:{version}"],
        cwd=ROOT, env={**os.environ, "DATABASE_URL": "postgresql://unused@localhost/unused"}, capture_output=True,
    ).returncode == 0


async def check(settings: dict[str, str]) -> str | None:
    """Prints what's on the database. Returns its migration version."""
    conn = await connect(settings)
    try:
        await conn.execute("SET default_transaction_read_only = on")
        tables = await conn.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
        has_alembic = await conn.fetchval("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        version = await conn.fetchval("SELECT version_num FROM alembic_version") if has_alembic else None
        print(f"Postgres {await conn.fetchval('SHOW server_version')}, {tables} tables, migration version: {version or 'none'}")
        # Open transactions hold locks that make schema changes wait
        sessions = await conn.fetch("""
            SELECT pid, state, date_trunc('second', now() - xact_start) AS open_for, wait_event_type, wait_event,
                   pg_blocking_pids(pid) AS blocked_by, left(regexp_replace(query, '\\s+', ' ', 'g'), 120) AS query
            FROM pg_stat_activity
            WHERE datname = current_database() AND pid <> pg_backend_pid() AND xact_start IS NOT NULL
              AND now() - xact_start > interval '30 seconds'
            ORDER BY xact_start
        """)
        for s in sessions:
            waiting = f", waiting on {s['wait_event_type']}/{s['wait_event']}" if s["wait_event_type"] else ""
            blocked = f", blocked by {list(s['blocked_by'])}" if s["blocked_by"] else ""
            print(f"  Open transaction pid {s['pid']}: {s['state']} for {s['open_for']}{waiting}{blocked}\n    {s['query']}")
        # Is this our schema? Compare its tables with the local database's
        columns_sql = "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"
        prod_cols = {(r[0], r[1]) for r in await conn.fetch(columns_sql)}
        local_url = local_database_url()
        if local_url:
            local = await asyncpg.connect(local_url, timeout=10)
            try:
                local_cols = {(r[0], r[1]) for r in await local.fetch(columns_sql)}
            finally:
                await local.close()
            prod_tables, local_tables = {t for t, _ in prod_cols}, {t for t, _ in local_cols}
            shared = prod_tables & local_tables
            print(f"  Tables only on prod: {sorted(prod_tables - local_tables) or 'none'}")
            print(f"  Tables only on local: {sorted(local_tables - prod_tables) or 'none'}")
            print(f"  Columns only on prod: {sorted(f'{t}.{c}' for t, c in prod_cols - local_cols if t in shared) or 'none'}")
            print(f"  Columns only on local: {sorted(f'{t}.{c}' for t, c in local_cols - prod_cols if t in shared) or 'none'}")
        if version in FOREIGN_VERSIONS:
            print(f"  Migration {version} is another copy's; setup lines it up with {FOREIGN_VERSIONS[version]} first.")
        elif version and not in_repo(version):
            print(f"  WARNING: migration {version} isn't in this repo. Don't run setup until you know where it came from.")
        return version
    finally:
        await conn.close()


def backup(settings: dict[str, str], db_name: str) -> Path:
    pg_dump = shutil.which("pg_dump") or next(
        (str(p) for p in sorted(Path("C:/Program Files/PostgreSQL").glob("*/bin/pg_dump.exe"), reverse=True)), None
    )
    if not pg_dump:
        sys.exit("pg_dump wasn't found, so no backup could be made. Install PostgreSQL's client tools and run again.")
    BACKUP_DIR.mkdir(exist_ok=True)
    target = BACKUP_DIR / f"{db_name}-{datetime.now():%Y%m%d-%H%M%S}.dump"
    env = {**os.environ, "PGSSLMODE": "require" if settings["DATABASE_SSL"].lower() in ("true", "require", "1") else "disable"}
    print(f"\nBacking up to {target} ...")
    subprocess.run([pg_dump, "--format=custom", "--no-owner", "--schema=public", f"--file={target}", f"--dbname={settings['DATABASE_URL']}"], env=env, check=True)
    print(f"Backup done ({target.stat().st_size // 1024} KB). Restore with: pg_restore --clean --no-owner -d <url> \"{target}\"")
    return target


async def line_up(settings: dict[str, str], version: str) -> None:
    """Brings a database made by another copy of the template in line with this repo, at the matching version."""
    conn = await connect(settings)
    try:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}")
            existing = {(r[0], r[1]) for r in await conn.fetch(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"
            )}
            for table, columns in MISSING_COLUMNS.items():
                missing = [c for c in columns if (table, c) not in existing]
                if missing:
                    print(f"Adding {table} columns: {', '.join(missing)}")
                    await conn.execute(f"ALTER TABLE {table} " + ", ".join(f"ADD COLUMN {c} {columns[c]}" for c in missing))
            # Required columns with no default that this app doesn't know about would make its inserts fail
            required = await conn.fetch("""
                SELECT c.table_name, c.column_name FROM information_schema.columns c
                WHERE c.table_schema = 'public' AND c.table_name = ANY($1::text[]) AND c.is_nullable = 'NO'
                  AND c.column_default IS NULL AND c.is_identity = 'NO'
            """, INSERTED_TABLES)
            local_url = local_database_url()
            if not local_url:
                raise SystemExit("Set LOCAL_DATABASE_URL in .env: lining up compares with the local database.")
            local = await asyncpg.connect(local_url, timeout=10)
            try:
                ours = {(r[0], r[1]) for r in await local.fetch(
                    "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"
                )}
            finally:
                await local.close()
            for r in required:
                if (r["table_name"], r["column_name"]) not in ours:
                    print(f"Making {r['table_name']}.{r['column_name']} optional (this app doesn't fill it in)")
                    await conn.execute(f'ALTER TABLE {r["table_name"]} ALTER COLUMN "{r["column_name"]}" DROP NOT NULL')
            await conn.execute("UPDATE alembic_version SET version_num = $1 WHERE version_num = $2", FOREIGN_VERSIONS[version], version)
            print(f"Migration version {version} -> {FOREIGN_VERSIONS[version]}")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="read-only: show what's there and stop")
    parser.add_argument("--no-backup", action="store_true", help="skip the pg_dump backup (not recommended)")
    parser.add_argument("--sample-data", action="store_true", help="also add the mock initiatives, demo events and demo ads")
    args = parser.parse_args()

    settings = prod_settings()
    target = urlsplit(settings["DATABASE_URL"])
    db_name = target.path.lstrip("/")
    print(f"Production database: {db_name} on {target.hostname}")
    version = asyncio.run(check(settings))
    if args.check:
        return
    if version and version not in FOREIGN_VERSIONS and not in_repo(version):
        sys.exit("Stopped: the database's migration version isn't in this repo, so the setup could change another app's data.")

    if input(f"Type the database name ({db_name}) to set up its schema: ").strip() != db_name:
        sys.exit("Cancelled. Nothing was changed.")
    if not args.no_backup:
        backup(settings, db_name)
    if version in FOREIGN_VERSIONS:
        print()
        asyncio.run(line_up(settings, version))
    env = {**os.environ, **settings}
    for step in STEPS:
        print(f"\n> {' '.join(step[2:])}")
        subprocess.run(step, cwd=ROOT, env=env, check=True)
    if args.sample_data:
        if input("Add the mock data (sample initiatives, demo events and ads) to this database? Type yes: ").strip() != "yes":
            sys.exit("Mock data not added. The schema is up to date.")
        sample_env = {**env, "ALLOW_SAMPLE_DATA_HOST": target.hostname or ""}
        for step in SAMPLE_STEPS:
            print(f"\n> {' '.join(step[1:])}")
            subprocess.run(step, cwd=ROOT, env=sample_env, check=True)
    print()
    asyncio.run(check(settings))
    print("Done.")


if __name__ == "__main__":
    main()
