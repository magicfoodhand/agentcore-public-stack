"""OIDC UserInfo endpoint client for enriching user profiles.

When a JWT access token lacks profile claims (email, name, picture),
this module fetches them from the provider's standard OIDC userinfo
endpoint. Results are cached per user_id with a TTL to avoid hitting
the endpoint on every request.
"""

import logging
import time
from typing import Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# Cache: user_id -> (claims_dict, timestamp)
_userinfo_cache: Dict[str, Tuple[dict, float]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes


def _get_cached(user_id: str) -> Optional[dict]:
    """Return cached userinfo claims if still valid, else None."""
    entry = _userinfo_cache.get(user_id)
    if entry is None:
        return None
    claims, ts = entry
    if time.monotonic() - ts > _CACHE_TTL_SECONDS:
        del _userinfo_cache[user_id]
        return None
    return claims


def _set_cached(user_id: str, claims: dict) -> None:
    """Store userinfo claims in the cache."""
    _userinfo_cache[user_id] = (claims, time.monotonic())


async def fetch_userinfo(
    userinfo_endpoint: str,
    access_token: str,
    user_id: str,
) -> Optional[dict]:
    """
    Fetch user profile claims from the OIDC userinfo endpoint.

    Args:
        userinfo_endpoint: The provider's userinfo URL.
        access_token: The Bearer token to present.
        user_id: Used as cache key.

    Returns:
        Dict of claims from the userinfo response, or None on failure.
    """
    # Check cache first
    cached = _get_cached(user_id)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            claims = resp.json()
            _set_cached(user_id, claims)
            logger.debug(f"Fetched userinfo for user {user_id}")
            return claims
    except Exception as e:
        logger.warning(f"Failed to fetch userinfo for user {user_id}: {e}")
        return None


def enrich_user_from_userinfo(user, claims: dict, provider) -> None:
    """
    Fill in missing User fields from userinfo claims using the
    provider's claim mappings. Mutates the user object in place.

    Args:
        user: The auth User dataclass instance.
        claims: Dict returned by the userinfo endpoint.
        provider: The AuthProvider with claim mapping config.
    """
    if not user.email and claims:
        email = (
            claims.get(provider.email_claim)
            or claims.get("email")
            or claims.get("preferred_username")
            or claims.get("upn")
        )
        if email:
            user.email = str(email).lower()

    if not user.name and claims:
        name = claims.get(provider.name_claim) or claims.get("name")
        if not name and provider.first_name_claim and provider.last_name_claim:
            first = claims.get(provider.first_name_claim, "")
            last = claims.get(provider.last_name_claim, "")
            name = f"{first} {last}".strip()
        if name:
            user.name = str(name)

    if not user.picture and claims:
        pic_claim = provider.picture_claim or "picture"
        picture = claims.get(pic_claim)
        if picture:
            user.picture = picture
