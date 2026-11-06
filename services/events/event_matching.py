"""Matching an event (from Navan) to the HubSpot campaign and marketing event of the same name.

Names are compared by their distinctive words: the brand code ("DS -", "TS - POS -"), years and words every event name
has ("show", "expo", "trade", "summit"...) are ignored. "NexusCon" matches "DS - NexusCon 2026"; "Tampa Show" matches
"Tampa Show 2026" but not "DS - Farm Progress Show". A match is only made when it's clear: one best candidate, with
all the distinctive words of the shorter name in the longer one (a single word must be at least 5 letters), or a close
overlap. Anything else is left for someone to link by hand.
"""
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Words that say what kind of event it is, not which one
_GENERIC = frozenset(
    "show shows expo event events evt trade tradeshow tradeshows conference summit exhibition exhibitions visit visits "
    "annual the and of for at in pos leads pre post fair forum congress booth webinar meeting launch".split()
)
# Brand codes at the start of HubSpot campaign names (services/hubspot/hubspot_campaign_sync_job.CAMPAIGN_BRANDS)
_BRAND_CODES = frozenset("ds daas zd zt ts wa au df os k9 jd zo".split())
_YEAR = re.compile(r"^(19|20)\d{2}$")
_NON_WORD = re.compile(r"[^a-z0-9]+")
CLOSE_OVERLAP = 0.6
MIN_SINGLE_WORD = 5
# A runner-up this close to the best match makes it ambiguous
AMBIGUOUS_GAP = 0.05


def words(name: str | None) -> frozenset[str]:
    """The distinctive words of an event name (all its words when it has none, e.g. "The Show")."""
    tokens = [t for t in _NON_WORD.split((name or "").lower()) if len(t) >= 3 and not _YEAR.match(t)]
    core = [t for t in tokens if t not in _GENERIC and t not in _BRAND_CODES]
    return frozenset(core or [t for t in tokens if t not in _BRAND_CODES])


def score(a: frozenset[str], b: frozenset[str]) -> float:
    """0 when the names don't match, else up to 1 (the same distinctive words)."""
    if not a or not b:
        return 0.0
    shared = a & b
    if not shared:
        return 0.0
    if a == b:
        return 1.0
    smaller = a if len(a) <= len(b) else b
    if smaller <= (a | b) and smaller == shared and (len(shared) >= 2 or len(next(iter(shared))) >= MIN_SINGLE_WORD):
        return 0.9
    jaccard = len(shared) / len(a | b)
    return round(jaccard, 2) if jaccard >= CLOSE_OVERLAP else 0.0


def best_match(name: str, candidates: Iterable[Tuple[str, str]]) -> Optional[Tuple[str, float]]:
    """(id, score) of the one clear best candidate (id, name) for the name, or None."""
    mine = words(name)
    scored: List[Tuple[float, str]] = sorted(
        ((score(mine, words(candidate_name)), candidate_id) for candidate_id, candidate_name in candidates), reverse=True
    )
    scored = [s for s in scored if s[0] > 0]
    if not scored:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < AMBIGUOUS_GAP:
        return None
    return scored[0][1], scored[0][0]


def match_list(name: str, lists: Iterable[Tuple[str, str]]) -> Optional[Tuple[str, float]]:
    """(id, score) of the one contact list (id, name) that clearly belongs to the event, or None."""
    return best_match(name, lists)


def match_event(
    name: str,
    campaigns: List[Dict[str, Any]],
    marketing_events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """{"campaign_id", "marketing_event_id", "score"} found from the names; ids are None where nothing matches.

    A matched campaign that has a marketing event among its assets gives that event when the names found none."""
    campaign = best_match(name, ((c["id"], c["name"]) for c in campaigns))
    event = best_match(name, ((e["id"], e["name"]) for e in marketing_events))
    campaign_id = campaign[0] if campaign else None
    event_id = event[0] if event else None
    if campaign_id and not event_id:
        known = {e["id"] for e in marketing_events}
        chosen = next((c for c in campaigns if c["id"] == campaign_id), None) or {}
        for asset in (chosen.get("assets") or {}).get("MARKETING_EVENT") or []:
            if isinstance(asset, dict) and str(asset.get("id")) in known:
                event_id = str(asset["id"])
                break
    scores = [m[1] for m in (campaign, event) if m]
    return {"campaign_id": campaign_id, "marketing_event_id": event_id, "score": max(scores) if scores else None}
