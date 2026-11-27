"""On-demand Google reports, independently failing sources, and a bounded five-minute cache."""
import asyncio
import time
from collections import OrderedDict
from datetime import date, datetime, timezone

import httpx

from models.marketing.google_reporting_model import GoogleMarketingReport, GoogleReportSection
from services.marketing.google_report_readers import read_ads, read_ga4, read_search_console
from services.marketing.google_reporting_accounts import ACCOUNTS, reporting_brands
from services.shared.core.google_reader_auth_service import GoogleReaderAuthError

_cache: OrderedDict[tuple, tuple[float, GoogleMarketingReport]] = OrderedDict()
_pending: dict[tuple, asyncio.Task] = {}
_CACHE_SECONDS = 300
_MAX_REPORTS = 64


async def _section(reader, client, account, start, end) -> GoogleReportSection:
    try:
        async with asyncio.timeout(45):
            return await reader(client, account, start, end)
    except GoogleReaderAuthError:
        return GoogleReportSection(status="error", message="Reader sign-in or impersonation failed. Check the backend's ADC and reader permissions.")
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        message = "Google rejected this report. Check this source's account access and API configuration."
        if code == 429:
            message = "Google's request quota was reached. Try again later."
        return GoogleReportSection(status="error", message=f"{message} (HTTP {code})")
    except (httpx.RequestError, TimeoutError):
        return GoogleReportSection(status="error", message="Google could not be reached in time. Try again later.")
    except (ValueError, KeyError, TypeError, IndexError):
        return GoogleReportSection(status="error", message="Google returned an unexpected report format.")


async def _fetch_report(brand: str, start: date, end: date) -> GoogleMarketingReport:
    account = ACCOUNTS[brand]
    async with httpx.AsyncClient(timeout=30) as client:
        sections = await asyncio.gather(*(
            _section(reader, client, account, start, end)
            for reader in (read_ga4, read_search_console, read_ads)
        ))
    return GoogleMarketingReport(
        brand=next(b for b in reporting_brands() if b.code == brand), date_from=start, date_to=end,
        fetched_at=datetime.now(timezone.utc), ga4=sections[0], search_console=sections[1], ads=sections[2],
    )


async def get_google_report(brand: str, start: date, end: date) -> GoogleMarketingReport:
    """Coalesce concurrent requests; never store credentials or reports on disk."""
    key = (brand, start, end)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    if key not in _pending:
        if len(_pending) >= 8:
            raise RuntimeError("Google reporting is busy; try again shortly.")
        task = asyncio.create_task(_fetch_report(brand, start, end))
        _pending[key] = task
        task.add_done_callback(lambda done: _finish_report(key, done))
    return await asyncio.shield(_pending[key])


def _finish_report(key: tuple, task: asyncio.Task) -> None:
    _pending.pop(key, None)
    if task.cancelled() or task.exception() is not None:
        return
    report = task.result()
    # Don't retain transient errors for five minutes.
    if any(section.status == "error" for section in (report.ga4, report.search_console, report.ads)):
        return
    _cache[key] = (time.monotonic(), report)
    _cache.move_to_end(key)
    while len(_cache) > _MAX_REPORTS:
        _cache.popitem(last=False)
