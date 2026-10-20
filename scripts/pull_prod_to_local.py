"""Copy production into the local Docker database: one way, prod -> local. Production is only read.

    venv/Scripts/python.exe scripts/pull_prod_to_local.py      (or scripts/pull_prod_to_local.ps1 / .sh)

1. Starts the local database container (docker compose up -d db).
2. Dumps production's app tables (the public schema; Supabase's own schemas are left out) with pg_dump, which only
   reads. The source is PROD_DATABASE_URL in .env.
3. Replaces the local public schema with that copy, so local matches production exactly.
4. Runs `alembic upgrade head` locally, so migrations on your branch that aren't on production yet are applied on top.

Everything local is replaced, including test and sample data. Nothing is ever written to production.
"""
import os
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTAINER = "marketing_db_local"
LOCAL_DB = "marketing_portal_dev"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "host.docker.internal"}
DUMP = "/tmp/prod.dump"


def docker(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], cwd=ROOT, check=check, capture_output=capture, text=True)


def psql(sql: str) -> str:
    return docker("exec", CONTAINER, "psql", "-U", "postgres", "-d", LOCAL_DB, "-tAc", sql, capture=True).stdout.strip()


def source_url(env: dict) -> str:
    url = env.get("PROD_DATABASE_URL") or ""
    if not url and urlsplit(env.get("DATABASE_URL") or "").hostname not in LOCAL_HOSTS:
        url = env.get("DATABASE_URL") or ""  # older setups kept production in DATABASE_URL
    if not url or urlsplit(url).hostname in LOCAL_HOSTS:
        sys.exit("Set PROD_DATABASE_URL in .env to the production database.")
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    if (env.get("PROD_DATABASE_SSL") or "true").lower() in ("true", "require", "1"):
        query.setdefault("sslmode", "require")
    return urlunsplit(parts._replace(query=urlencode(query)))


def wait_until_ready() -> None:
    for _ in range(30):
        if docker("exec", CONTAINER, "pg_isready", "-U", "postgres", "-d", LOCAL_DB, check=False, capture=True).returncode == 0:
            return
        time.sleep(1)
    sys.exit(f"The local database ({CONTAINER}) didn't start. Check Docker Desktop is running.")


def main() -> None:
    env = dotenv_values(os.path.join(ROOT, ".env"))
    url = source_url(env)
    source = urlsplit(url)
    print("=" * 64)
    print(" Production -> local database (one way: production is only read)")
    print("=" * 64)
    print(f"From: {source.path.lstrip('/')} on {source.hostname}")
    print(f"To:   {LOCAL_DB} in the {CONTAINER} container")

    print("\n[1/4] Starting the local database...")
    docker("compose", "up", "-d", "db", capture=True)
    wait_until_ready()

    print("[2/4] Copying production (read-only)...")
    dumped = docker("exec", CONTAINER, "pg_dump", url, "--schema=public", "--no-owner", "--no-acl", "--format=custom", f"--file={DUMP}",
                    check=False, capture=True)
    if dumped.returncode != 0:
        if "server version mismatch" in dumped.stderr:
            sys.exit("The local Postgres is older than production's. Update the image in docker-compose.yml to production's "
                     "version and run this again.\n" + dumped.stderr.strip())
        sys.exit("Couldn't copy production:\n" + dumped.stderr.strip())

    print("[3/4] Replacing the local database with the copy...")
    psql("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public")
    restored = docker("exec", CONTAINER, "pg_restore", "-U", "postgres", "-d", LOCAL_DB, "--no-owner", "--no-acl", DUMP,
                      check=False, capture=True)
    docker("exec", CONTAINER, "rm", "-f", DUMP, check=False, capture=True)
    # Expected and harmless: the empty schema already exists, and Supabase's own roles (in its row security
    # policies) don't exist in a plain Postgres. Anything else is shown
    harmless = ('schema "public" already exists', "role ", "does not exist")
    errors = [line for line in restored.stderr.splitlines()
              if "error:" in line.lower() and not (harmless[0] in line or (harmless[1] in line and harmless[2] in line))]
    if errors:
        print(f"  {len(errors)} restore warning(s):")
        for line in errors[:10]:
            print(f"    {line.strip()}")

    print("[4/4] Applying migrations from your branch that production doesn't have yet...")
    local_url = f"postgresql://postgres:postgres@localhost:{local_port()}/{LOCAL_DB}"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True,
                   env={**os.environ, "DATABASE_URL": local_url, "DATABASE_SSL": "false"})

    tables = psql("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
    version = psql("SELECT version_num FROM alembic_version") if tables != "0" else "none"
    initiatives = psql("SELECT count(*) FROM initiatives") if tables != "0" else "0"
    print(f"\nDone. Local now has {tables} tables at migration {version}, with {initiatives} initiatives.")
    print(f"Connect at {local_url} (e.g. DataGrip). Production wasn't changed.")


def local_port() -> str:
    mapping = docker("port", CONTAINER, "5432/tcp", capture=True).stdout.splitlines()
    return mapping[0].rsplit(":", 1)[1] if mapping else "5432"


if __name__ == "__main__":
    main()
