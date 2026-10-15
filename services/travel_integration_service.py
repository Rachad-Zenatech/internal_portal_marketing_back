"""
Travel Integration Service
Machine-to-machine calls from this portal to the travel management system, following the same pattern the
M&A portal uses to call Admin: a scoped service token, tracing headers (X-Source-Service, X-Request-ID,
X-Correlation-ID), a circuit breaker, a last-known-good cache for reads, and a refresh when the travel
system comes back.

Unlike that reference, nothing here is hard-coded: the travel system's address comes from TRAVEL_BACKEND_URL,
the token is signed with the app's own JWT settings (services/service_security_policy.py) and carries only the
scopes it needs, and this portal never reads the travel system's database.
"""

import asyncio
import datetime
import logging
import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from pydantic import ValidationError

from models.travel_integration_model import TravelCostsResponse
from services.logging_service import request_id_ctx
from services.service_security_policy import generate_scoped_outbound_token

logger = logging.getLogger(__name__)

TRAVEL_TARGET_SERVICE = "travel-management"
SOURCE_SERVICE = "marketing"
SOURCE_SERVICE_HEADER = "marketing-portal"
TRAVEL_COSTS_SCOPE = "travel:costs_read"
TRAVEL_COSTS_PATH = "/api/travel/integrations/marketing/costs"

CONNECT_TIMEOUT = 3.0
DEFAULT_TIMEOUT_SECONDS = 10.0
TOKEN_LIFETIME_MINUTES = 60
# A cached token is replaced this long before it expires
TOKEN_REFRESH_MARGIN_SECONDS = 300


def travel_backend_url() -> Optional[str]:
    """The travel system's base URL, or None when it isn't configured. Read on each call so tests and
    reloads see changes."""
    value = (os.getenv("TRAVEL_BACKEND_URL") or "").strip().rstrip("/")
    return value or None


def travel_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("TRAVEL_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


class TravelUnavailable(Exception):
    """The travel system can't be used right now (not configured, unreachable, failing, or circuit open).
    The message is safe to show to users."""


class TravelResponseError(TravelUnavailable):
    """The travel system answered, but not with what the contract says."""


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class CircuitState:
    CLOSED = "CLOSED"        # Known reachable: calls go through
    OPEN = "OPEN"            # Known unreachable: calls fail fast without a network round trip
    HALF_OPEN = "HALF_OPEN"  # Recovery time has passed: the next call is a probe


StateChangeListener = Callable[[str, str, str], None]


class TravelCircuitBreaker:
    def __init__(
        self,
        name: str = "travel",
        failure_threshold: int = 5,
        recovery_time_seconds: float = 30.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_time_seconds = recovery_time_seconds
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.last_state_change: float = time.time()
        self._listeners: List[StateChangeListener] = []

    def add_listener(self, listener: StateChangeListener) -> None:
        self._listeners.append(listener)

    def _transition_to(self, new_state: str) -> None:
        old_state = self.state
        if old_state == new_state:
            return
        self.state = new_state
        self.last_state_change = time.time()
        logger.info(
            f"[TravelCircuitBreaker:{self.name}] Transitioning {old_state} -> {new_state}",
            extra={"event": "travel_circuit_state_change", "old_state": old_state, "new_state": new_state},
        )
        for listener in self._listeners:
            try:
                listener(self.name, old_state, new_state)
            except Exception:
                logger.debug(f"[TravelCircuitBreaker:{self.name}] State-change listener error", exc_info=True)

    def current_state(self) -> str:
        """The state as callers should see it: OPEN turns HALF_OPEN once the recovery time has passed."""
        if (
            self.state == CircuitState.OPEN
            and self.last_failure_time is not None
            and (time.time() - self.last_failure_time) >= self.recovery_time_seconds
        ):
            return CircuitState.HALF_OPEN
        return self.state

    def allow_request(self) -> bool:
        if self.state == CircuitState.OPEN:
            if self.current_state() == CircuitState.HALF_OPEN:
                self._transition_to(CircuitState.HALF_OPEN)
                return True
            return False
        return True

    def record_success(self) -> None:
        self.failure_count = 0
        self.last_failure_time = None
        if self.state != CircuitState.CLOSED:
            self._transition_to(CircuitState.CLOSED)

    def record_failure(self, error: Exception) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.state == CircuitState.CLOSED and self.failure_count >= self.failure_threshold:
            logger.warning(
                f"[TravelCircuitBreaker:{self.name}] Threshold {self.failure_threshold} reached. "
                f"Transitioning CLOSED -> OPEN. Error: {error}"
            )
            self._transition_to(CircuitState.OPEN)
        elif self.state == CircuitState.HALF_OPEN:
            logger.warning(f"[TravelCircuitBreaker:{self.name}] Probe failed in HALF_OPEN. Re-opening: {error}")
            self._transition_to(CircuitState.OPEN)

    def reset(self) -> None:
        self.failure_count = 0
        self.last_failure_time = None
        self._transition_to(CircuitState.CLOSED)

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.current_state(),
            "failure_count": self.failure_count,
            "failure_threshold": self.failure_threshold,
            "last_failure_time": self.last_failure_time,
            "last_state_change": self.last_state_change,
            "target_url": travel_backend_url(),
        }


# ---------------------------------------------------------------------------
# Last-known-good cache
# ---------------------------------------------------------------------------

class ResilientCacheEntry:
    def __init__(self, data: Any):
        self.data = data
        self.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.timestamp = time.time()


class ResilientCache:
    """In-memory cache keeping the last good response of each read, for use while the travel system is down."""

    def __init__(self):
        self._cache: Dict[str, ResilientCacheEntry] = {}

    def set(self, key: str, data: Any) -> None:
        if data is not None:
            self._cache[key] = ResilientCacheEntry(data)

    def get(self, key: str) -> Tuple[Optional[Any], Optional[str]]:
        entry = self._cache.get(key)
        if entry:
            return entry.data, entry.updated_at
        return None, None

    def clear(self) -> None:
        self._cache.clear()


travel_circuit_breaker = TravelCircuitBreaker(name="travel_backend", failure_threshold=5, recovery_time_seconds=30.0)
resilient_cache = ResilientCache()

# Outcome of the most recent call, for travel_status()
_last_success_at: Optional[datetime.datetime] = None
_last_error: Optional[str] = None


def _note_success() -> None:
    global _last_success_at, _last_error
    _last_success_at = datetime.datetime.now(datetime.timezone.utc)
    _last_error = None


def _note_error(message: str) -> None:
    global _last_error
    _last_error = message


def travel_status() -> Dict[str, Any]:
    """{state: CLOSED|OPEN|HALF_OPEN, last_success_at, last_error} for the travel system connection."""
    return {
        "state": travel_circuit_breaker.current_state(),
        "last_success_at": _last_success_at,
        "last_error": _last_error,
    }


def reset_travel_integration_state() -> None:
    """Forget circuit, cache, token and status. For tests and admin tooling."""
    global _last_success_at, _last_error
    travel_circuit_breaker.reset()
    resilient_cache.clear()
    _service_token_cache.clear()
    _last_success_at = None
    _last_error = None


# ---------------------------------------------------------------------------
# Service token (least privilege: only the scopes asked for, no user or admin claims)
# ---------------------------------------------------------------------------

_service_token_cache: Dict[Tuple[str, ...], Tuple[str, float]] = {}


def get_travel_service_token(scopes: List[str]) -> str:
    """A scoped service token for the travel system, reused until shortly before it expires."""
    key = tuple(sorted(scopes))
    now_ts = time.time()
    cached = _service_token_cache.get(key)
    if cached and cached[1] > now_ts + TOKEN_REFRESH_MARGIN_SECONDS:
        return cached[0]
    token = generate_scoped_outbound_token(
        target_service=TRAVEL_TARGET_SERVICE,
        requested_scopes=list(key),
        source_service=SOURCE_SERVICE,
        expires_in_minutes=TOKEN_LIFETIME_MINUTES,
    )
    _service_token_cache[key] = (token, now_ts + TOKEN_LIFETIME_MINUTES * 60)
    return token


# ---------------------------------------------------------------------------
# Authenticated HTTP transport
# ---------------------------------------------------------------------------

# Tests swap this for an httpx.MockTransport; None means real network calls
_transport: Optional[httpx.AsyncBaseTransport] = None


def _client(timeout_seconds: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds, connect=min(CONNECT_TIMEOUT, timeout_seconds)),
        transport=_transport,
    )


def _from_cache(cache_key: Optional[str], path: str) -> Optional[Any]:
    if not cache_key:
        return None
    cached_data, cached_at = resilient_cache.get(cache_key)
    if cached_data is not None:
        logger.info(f"[TravelIntegration] Serving last-known-good response for {path} (cached_at: {cached_at})")
    return cached_data


async def _make_travel_request(
    method: str,
    path: str,
    scopes: List[str],
    params: Optional[Dict[str, Any]] = None,
    json_data: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    cache_key: Optional[str] = None,
) -> Any:
    """Call the travel system and return the decoded JSON body.

    - Distributed tracing: X-Request-ID (new for every call) and X-Correlation-ID (given, else this request's id)
    - Circuit breaker: fails fast while the travel system is known to be down
    - Last-known-good cache: a GET with a cache_key falls back to its last good response on failure

    Raises TravelUnavailable when the travel system isn't configured, can't be reached, or answers with an error
    and no cached response can stand in.
    """
    base_url = travel_backend_url()
    if not base_url:
        raise TravelUnavailable("The travel system is not set up. Ask an administrator to set TRAVEL_BACKEND_URL.")

    is_read = method.upper() == "GET"
    read_cache_key = cache_key if is_read else None

    if not travel_circuit_breaker.allow_request():
        cached = _from_cache(read_cache_key, path)
        if cached is not None:
            return cached
        logger.warning(f"[TravelIntegration] Circuit breaker OPEN for {path}. Fast-rejecting request.")
        raise TravelUnavailable("The travel system is not responding. Try again in a few minutes.")

    request_id = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {get_travel_service_token(scopes)}",
        "Accept": "application/json",
        "X-Source-Service": SOURCE_SERVICE_HEADER,
        "X-Request-ID": request_id,
        "X-Correlation-ID": correlation_id or request_id_ctx.get() or str(uuid.uuid4()),
    }
    url = f"{base_url}{path}"
    log_extra = {"event": "travel_request", "method": method, "path": path, "request_id": request_id}

    try:
        async with _client(travel_timeout_seconds()) as client:
            resp = await client.request(method=method, url=url, params=params, json=json_data, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
        travel_circuit_breaker.record_failure(exc)
        _note_error(f"Could not reach the travel system: {type(exc).__name__}")
        logger.error(f"[TravelIntegration] Could not reach travel system at {url}: {exc}", extra=log_extra)
        cached = _from_cache(read_cache_key, path)
        if cached is not None:
            return cached
        raise TravelUnavailable("The travel system is not responding. Try again in a few minutes.") from exc

    if resp.status_code >= 500:
        travel_circuit_breaker.record_failure(Exception(f"HTTP {resp.status_code}"))
        _note_error(f"The travel system returned HTTP {resp.status_code}")
        logger.error(f"[TravelIntegration] Travel system error HTTP {resp.status_code} for {url}", extra=log_extra)
        cached = _from_cache(read_cache_key, path)
        if cached is not None:
            return cached
        raise TravelUnavailable("The travel system had a problem. Try again in a few minutes.")

    # A 4xx means the travel system is up; the request itself was refused
    travel_circuit_breaker.record_success()
    if resp.status_code >= 400:
        _note_error(f"The travel system refused the request with HTTP {resp.status_code}")
        logger.error(
            f"[TravelIntegration] Travel system refused {method} {path} with HTTP {resp.status_code}: {resp.text[:300]}",
            extra=log_extra,
        )
        if resp.status_code in (401, 403):
            raise TravelResponseError("The travel system did not accept this portal's credentials.")
        if resp.status_code == 404:
            raise TravelResponseError("The travel system does not offer travel costs yet.")
        raise TravelResponseError("The travel system refused the request.")

    try:
        data = resp.json()
    except ValueError as exc:
        _note_error("The travel system sent a response that is not JSON")
        raise TravelResponseError("The travel system sent a response this portal can't read.") from exc

    _note_success()
    if read_cache_key:
        resilient_cache.set(read_cache_key, data)
    return data


# ---------------------------------------------------------------------------
# Travel costs (proposed endpoint on the travel system)
# ---------------------------------------------------------------------------

def _iso_utc(value: datetime.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


async def fetch_travel_costs(
    updated_since: Optional[datetime.datetime],
    event_code: Optional[str] = None,
    correlation_id: Optional[str] = None,
    allow_cached: bool = False,
) -> TravelCostsResponse:
    """Approved travel costs (and later changes to them) from the travel system.

    GET {TRAVEL_BACKEND_URL}/api/travel/integrations/marketing/costs?updated_since=<ISO UTC>&event_code=<optional>

    allow_cached lets a read fall back to the last good response while the travel system is down. The cost sync
    leaves it off, so a sync never writes stale data or moves its cursor on an old answer.
    """
    params: Dict[str, Any] = {}
    if updated_since is not None:
        params["updated_since"] = _iso_utc(updated_since)
    if event_code:
        params["event_code"] = event_code
    cache_key = f"costs:{params.get('updated_since', '')}:{event_code or ''}" if allow_cached else None

    data = await _make_travel_request(
        "GET", TRAVEL_COSTS_PATH, [TRAVEL_COSTS_SCOPE],
        params=params or None, correlation_id=correlation_id, cache_key=cache_key,
    )
    try:
        return TravelCostsResponse.model_validate(data)
    except ValidationError as exc:
        _note_error("The travel system sent travel costs in an unexpected format")
        logger.error(f"[TravelIntegration] Travel costs response failed validation: {exc}")
        raise TravelResponseError("The travel system sent travel costs in an unexpected format.") from exc


# ---------------------------------------------------------------------------
# Recovery refresh
# ---------------------------------------------------------------------------

async def _refresh_on_recovery() -> None:
    """When the travel system comes back, catch up on travel costs straight away (if the scheduled sync is on)
    instead of waiting for the next interval."""
    from services.travel_cost_sync_job import TravelSyncAlreadyRunning, sync_travel_costs, travel_cost_sync_enabled

    if not travel_cost_sync_enabled():
        return
    logger.info("[TravelRecovery] Travel system back online. Running a travel cost sync.")
    try:
        await sync_travel_costs()
    except TravelSyncAlreadyRunning:
        logger.info("[TravelRecovery] Catch-up sync skipped: a travel sync is already running.")
    except Exception as exc:
        logger.warning(f"[TravelRecovery] Catch-up sync failed: {exc}", extra={"error": str(exc)})


# The pending catch-up task, if any (at most one), kept referenced until it finishes
_recovery_tasks: set = set()


def _on_circuit_state_change(name: str, old_state: str, new_state: str) -> None:
    if old_state not in (CircuitState.OPEN, CircuitState.HALF_OPEN) or new_state != CircuitState.CLOSED:
        return
    from services.travel_cost_sync_job import travel_cost_sync_running

    # Usually the call that closed the circuit is a sync's own fetch: that sync is the catch-up
    if travel_cost_sync_running():
        logger.info("[TravelRecovery] CircuitBreaker closed again during a travel sync. No catch-up sync needed.")
        return
    if any(not task.done() for task in _recovery_tasks):
        logger.info("[TravelRecovery] CircuitBreaker closed again. A catch-up sync is already pending.")
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    logger.info("[TravelRecovery] CircuitBreaker closed again. Scheduling catch-up sync.")
    task = loop.create_task(_refresh_on_recovery(), name="travel-recovery-sync")
    _recovery_tasks.add(task)
    task.add_done_callback(_recovery_tasks.discard)


async def cancel_recovery_tasks() -> None:
    """Cancel pending catch-up syncs and wait for them to finish. Call on shutdown."""
    tasks = [task for task in _recovery_tasks if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _recovery_tasks.clear()


def _register_travel_recovery_listener() -> None:
    travel_circuit_breaker.add_listener(_on_circuit_state_change)


_register_travel_recovery_listener()
