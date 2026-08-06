"""Pure parsing and coercion helpers for the HelloFresh integration.

These functions are stateless and side-effect free. They are deliberately kept
separate from the HTTP/orchestration logic in client.py so they can be reused and
unit-tested in isolation.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Sequence
from datetime import date, datetime
import json
import re
from typing import Any
from urllib.parse import unquote, urlparse

# Maximum recursion depth when heuristically searching nested payloads.
MAX_SEARCH_DEPTH = 8

_CARRIER_LABELS = {
    "DDASH": "DoorDash",
    "FEDEX": "FedEx",
    "UPS": "UPS",
    "USPS": "USPS",
    "ONTRAC": "OnTrac",
    "LASERSHIP": "LaserShip",
}

_RECIPE_HEADING_DISALLOWED = {
    "our plans",
    "about us",
    "our menus",
    "help center",
    "gift cards",
    "premium picks",
    "20 min or less",
    "bistro night",
    "build-a-plate",
    "prep & bake",
    "test kitchen",
    "new",
    "recipes",
    "log in",
}


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def parse_date(value: Any) -> date | None:
    """Parse a date-ish value."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None


_ISO_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def date_from_iso_week(week_id: str | None) -> date | None:
    """Return the delivery date for a ``YYYY-Www`` ISO week, or ``None``.

    Some HelloFresh history payloads (notably ``/gw/my-deliveries/past-deliveries``) identify a
    delivered week by its ISO week id only, with no explicit delivery date. The delivery falls in
    that ISO week; HelloFresh's own UI labels it with the week's Monday, so use ISO weekday 1.
    """
    if not isinstance(week_id, str):
        return None
    match = _ISO_WEEK_RE.match(week_id.strip())
    if not match:
        return None
    try:
        return date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError:
        return None  # out-of-range week number


def iso_week_label(week_id: str | None, fallback: date | None = None) -> str | None:
    """Return the ISO week identifier for a delivery week (e.g. ``"2026-W25"``).

    Prefers the HelloFresh ``week_id`` when it is already a valid ``YYYY-Www`` ISO week;
    otherwise derives the label from ``fallback``'s ISO calendar week (typically the week's
    delivery date). Returns ``None`` when neither yields a usable week.
    """
    if isinstance(week_id, str):
        match = _ISO_WEEK_RE.match(week_id.strip())
        if match:
            year, week = int(match.group(1)), int(match.group(2))
            try:
                date.fromisocalendar(year, week, 1)  # validate the week number is in range
            except ValueError:
                pass  # out-of-range week number; fall through to the date fallback
            else:
                return f"{year:04d}-W{week:02d}"
    if fallback is not None:
        iso = fallback.isocalendar()
        return f"{iso.year:04d}-W{iso.week:02d}"
    return None


def parse_datetime(value: Any) -> datetime | None:
    """Parse a datetime-ish value."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def decode_jwt_claims(token: str | None) -> dict[str, Any] | None:
    """Decode a JWT's payload claims without verifying the signature.

    Returns the claims dict, or None when the value is not a well-formed JWT.
    Used only to read self-asserted ``iat``/``exp`` timing for diagnostics; the
    integration never trusts these claims for authorization decisions.
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_segment = parts[1]
    padding = "=" * (-len(payload_segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_segment + padding)
        claims = json.loads(decoded)
    except (binascii.Error, ValueError):
        return None
    return claims if isinstance(claims, dict) else None


_MAX_URL_DECODE_PASSES = 3


def loads_possibly_url_encoded(text: str | None) -> Any:
    """Parse JSON that may be plain, URL-encoded, or double-URL-encoded.

    The browser exposes the ``apiV2Auth`` cookie value, which users commonly paste in its
    URL-encoded form (``%7B%22access_token%22...``). A plain ``json.loads`` fails on that, so
    we retry after ``unquote`` up to a few times to recover from single or double encoding.
    Returns the decoded object, or ``None`` when nothing parses.
    """
    if not text:
        return None
    candidate = text.strip()
    for _ in range(_MAX_URL_DECODE_PASSES):
        try:
            return json.loads(candidate)
        except ValueError:
            decoded = unquote(candidate)
            if decoded == candidate:
                # No further decoding is possible; the value is not JSON.
                return None
            candidate = decoded
    return None


def token_payload_to_entry_data(text: str | None) -> dict[str, Any] | None:
    """Map a pasted token blob to config-entry token fields.

    Accepts either a full ``apiV2Auth`` auth object (``{"access_token", "refresh_token",
    "expires_in", "refresh_expires_in", "token_type"}``, plain or URL-encoded) or a bare
    access-token string. Returns a dict keyed by the integration's CONF_* token keys, or
    ``None`` when no access token can be recovered. Timing fields absent from the payload are
    backfilled from the access token's own JWT ``iat``/``exp`` claims so the expiry sensors
    and proactive refresh still work.

    The returned keys deliberately match ``const.CONF_*`` string values so callers can store
    the dict straight into ``entry.data``.
    """
    if not text:
        return None

    payload = loads_possibly_url_encoded(text)
    if isinstance(payload, dict):
        access_token = payload.get("access_token")
    else:
        # Not a JSON object: treat the raw input as a bare access token.
        access_token = text.strip()
        payload = {}

    if not isinstance(access_token, str) or not access_token.strip():
        return None
    access_token = access_token.strip()

    claims = decode_jwt_claims(access_token) or {}
    issued_at = coerce_int(payload.get("issued_at")) or coerce_int(claims.get("iat"))
    expires_in = coerce_int(payload.get("expires_in"))
    if expires_in is None:
        exp = coerce_int(claims.get("exp"))
        if exp is not None and issued_at is not None:
            expires_in = exp - issued_at

    refresh_token = payload.get("refresh_token")
    refresh_token = refresh_token.strip() if isinstance(refresh_token, str) else None

    data: dict[str, Any] = {"access_token": access_token}
    if refresh_token:
        data["refresh_token"] = refresh_token
    if issued_at is not None:
        data["issued_at"] = issued_at
        # The refresh token's own 60-day clock is anchored to the same issue time when the
        # blob does not carry an explicit refresh issue time.
        data["refresh_token_issued_at"] = (
            coerce_int(payload.get("refresh_token_issued_at")) or issued_at
        )
    if expires_in is not None:
        data["expires_in"] = expires_in
    refresh_expires_in = coerce_int(payload.get("refresh_expires_in"))
    if refresh_expires_in is not None:
        data["refresh_expires_in"] = refresh_expires_in
    token_type = payload.get("token_type")
    if isinstance(token_type, str) and token_type.strip():
        data["token_type"] = token_type.strip()
    return data


def coerce_int(value: Any) -> int | None:
    """Coerce an integer-ish value."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_localized_number(text: str) -> str | None:
    """Reduce a localized amount string to plain ``float()`` syntax.

    Comma-decimal HelloFresh markets (DE, NL, FR, ...) send amounts like ``"89,92"``,
    ``"1.234,56"``, or symbol-laden labels like ``"€ 8,99"``; dot-decimal markets may
    send ``"1,234.56"``. Strip everything but digits, sign, and separators, then decide
    which separator is the decimal point: when both appear, the rightmost one wins;
    a lone comma is a decimal comma unless it is followed by exactly three digits
    (a thousands group like ``"1,234"``).
    """
    cleaned = re.sub(r"[^\d,.\-+]", "", text)
    if not re.search(r"\d", cleaned):
        return None
    last_comma = cleaned.rfind(",")
    last_dot = cleaned.rfind(".")
    if last_comma >= 0 and last_dot >= 0:
        if last_comma > last_dot:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif last_comma >= 0:
        head, _, tail = cleaned.rpartition(",")
        if "," not in head and len(tail) != 3:
            cleaned = f"{head}.{tail}"
        else:
            cleaned = cleaned.replace(",", "")
    return cleaned


def coerce_float(value: Any) -> float | None:
    """Coerce a float-ish value, including localized decimal-comma strings."""
    if value is None:
        return None
    if isinstance(value, str):
        value = _normalize_localized_number(value)
        if value is None:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    """Create a stable id from text."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:120]


def looks_like_recipe_heading(text: str) -> bool:
    """Return True when text looks like a recipe card heading."""
    if not text or len(text) < 6:
        return False
    lowered = text.lower()
    if lowered in _RECIPE_HEADING_DISALLOWED:
        return False
    return text[0].isalpha() and any(ch.islower() for ch in text)


def extract_menu_labels(page_text: str) -> list[str]:
    """Extract visible week labels from the public menu page."""
    labels: list[str] = []
    patterns = (
        r"Menu for ([A-Z][a-z]{2} \d{1,2} ?- ?[A-Z]?[a-z]{0,2}\d{0,2})",
        r"([A-Z][a-z]{2}(?:-[A-Z][a-z]{2})? \d{2}-\d{2})",
        r"([A-Z][a-z]{2} \d{2}-\d{2})",
    )
    for pattern in patterns:
        for match in re.findall(pattern, page_text):
            clean = " ".join(match.split())
            if clean not in labels:
                labels.append(clean)
    return labels[:6]


# ---------------------------------------------------------------------------
# Recipe / week field parsers
# ---------------------------------------------------------------------------


def extract_name_list(raw_value: Any) -> list[str]:
    """Normalize a list of strings or objects with names."""
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        return [raw_value]
    if not isinstance(raw_value, list):
        return []

    names: list[str] = []
    for item in raw_value:
        if isinstance(item, str):
            if item not in names:
                names.append(item)
            continue
        if isinstance(item, dict):
            value = item.get("name") or item.get("title") or item.get("label")
            if isinstance(value, str) and value not in names:
                names.append(value)
    return names


def extract_allowed_actions(raw_week: dict[str, Any]) -> dict[str, bool]:
    """Normalize HelloFresh allowedActions payloads into booleans."""
    allowed_actions = raw_week.get("allowedActions")
    if not isinstance(allowed_actions, dict):
        return {}
    return {str(key): bool(value) for key, value in allowed_actions.items() if isinstance(key, str)}


def normalize_candidate_dict_list(candidate: Any) -> list[dict[str, Any]]:
    """Return a list of dict items when the candidate is list-like."""
    if not isinstance(candidate, list):
        return []
    return [item for item in candidate if isinstance(item, dict)]


def find_nested_collection(
    node: Any,
    priority_keys: Sequence[str],
    predicate: Callable[[list[dict[str, Any]]], bool],
    *,
    dict_first: bool = True,
    _depth: int = 0,
) -> list[dict[str, Any]] | None:
    """Recursively search a payload for a dict-list matching ``predicate``.

    Walks dicts (preferring ``priority_keys``, then all values) and lists, returning
    the first normalized dict-list for which ``predicate`` is true, or ``None``.

    ``dict_first`` controls traversal order when a node is both a candidate and a
    container: menu/past-delivery searches inspect dict branches first, while the
    recipe search inspects list branches first. This preserves the original
    per-search precedence.
    """
    if _depth >= MAX_SEARCH_DEPTH:
        return None

    def search_dict(value: dict[str, Any]) -> list[dict[str, Any]] | None:
        for key in priority_keys:
            if key not in value:
                continue
            candidate = value[key]
            normalized = normalize_candidate_dict_list(candidate)
            if normalized and predicate(normalized):
                return normalized
            nested = find_nested_collection(
                candidate, priority_keys, predicate, dict_first=dict_first, _depth=_depth + 1
            )
            if nested:
                return nested
        for child in value.values():
            nested = find_nested_collection(
                child, priority_keys, predicate, dict_first=dict_first, _depth=_depth + 1
            )
            if nested:
                return nested
        return None

    def search_list(value: list[Any]) -> list[dict[str, Any]] | None:
        normalized = normalize_candidate_dict_list(value)
        if normalized and predicate(normalized):
            return normalized
        for item in value:
            nested = find_nested_collection(
                item, priority_keys, predicate, dict_first=dict_first, _depth=_depth + 1
            )
            if nested:
                return nested
        return None

    if dict_first:
        if isinstance(node, dict):
            return search_dict(node)
        if isinstance(node, list):
            return search_list(node)
    else:
        if isinstance(node, list):
            return search_list(node)
        if isinstance(node, dict):
            return search_dict(node)
    return None


def looks_like_recipe_collection(candidate: list[dict[str, Any]]) -> bool:
    """Heuristically identify recipe collections."""
    if not candidate:
        return False
    name_keys = {"name", "title", "slug"}
    detail_keys = {
        "headline",
        "description",
        "selected",
        "image",
        "imageUrl",
        "imagePath",
        "ingredients",
        "tags",
        "labels",
    }
    for item in candidate:
        nested_recipe = item.get("recipe") if isinstance(item.get("recipe"), dict) else None
        if (
            isinstance(nested_recipe, dict)
            and name_keys.intersection(nested_recipe)
            and ({"id"}.intersection(nested_recipe) or detail_keys.intersection(nested_recipe))
        ):
            return True
        if name_keys.intersection(item) and (
            {"id"}.intersection(item) or detail_keys.intersection(item)
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Tracking parsers
# ---------------------------------------------------------------------------


def normalize_carrier_name(carrier: str | None) -> str | None:
    """Map carrier codes into stable user-facing names when known."""
    if carrier is None:
        return None
    normalized = carrier.strip()
    if not normalized:
        return None
    return _CARRIER_LABELS.get(normalized.upper(), normalized)


def extract_tracking_details(raw_week: dict[str, Any]) -> dict[str, str | None]:
    """Extract shipment tracking details from a delivery payload."""
    tracking_nodes = [
        raw_week,
        raw_week.get("tracking") or {},
        raw_week.get("shipment") or {},
        raw_week.get("delivery") or {},
        raw_week.get("box") or {},
        raw_week.get("carrierTracking") or {},
    ]

    def pick(*keys: str) -> str | None:
        for node in tracking_nodes:
            if not isinstance(node, dict):
                continue
            for key in keys:
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    return {
        "tracking_url": pick(
            "trackingUrl",
            "trackingURL",
            "trackingLink",
            "tracking_link",
            "trackUrl",
            "trackURL",
            "tracking_link_url",
            "carrier_tracking_url",
            "public_url",
            "hf_tracking_url",
            "url",
        ),
        "tracking_number": pick(
            "trackingNumber",
            "trackingCode",
            "tracking_code",
            "tracking_id",
            "parcelNumber",
            "waybill",
            "consignmentNumber",
        ),
        "tracking_status": pick(
            "trackingStatus",
            "shipmentStatus",
            "tracking_status",
            "parcelStatus",
            "carrierStatus",
            "internal_status",
            "state",
        ),
        "carrier": normalize_carrier_name(
            pick(
                "carrier",
                "carrierName",
                "deliveryPartner",
                "provider",
                "shippingProvider",
            )
        ),
    }


def extract_tracking_public_id(tracking_url: str | None) -> str | None:
    """Return the public shipment tracking id from a HelloFresh tracking URL."""
    if not isinstance(tracking_url, str) or not tracking_url.strip():
        return None
    path = urlparse(tracking_url.strip()).path
    match = re.search(r"/delivery-tracking/([0-9a-fA-F-]{36})(?:/)?$", path)
    if match is None:
        return None
    return match.group(1)


def extract_scm_tracking_details(box: dict[str, Any]) -> dict[str, str | None]:
    """Extract carrier-facing tracking details from an SCM tracking box."""
    last_status = box.get("last_status") if isinstance(box.get("last_status"), dict) else {}

    def pick(node: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    tracking_url = pick(box, "carrier_tracking_url", "public_url") or pick(box, "hf_tracking_url")
    return {
        "tracking_url": tracking_url,
        "tracking_number": pick(box, "tracking_code", "tracking_id"),
        "tracking_status": (
            pick(last_status, "status", "internal_status") or pick(box, "status", "internal_status")
        ),
        "carrier": normalize_carrier_name(pick(box, "carrier")),
    }
