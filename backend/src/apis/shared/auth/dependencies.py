"""FastAPI dependencies for authentication."""

import asyncio
import jwt
import logging
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .models import User
from .userinfo import fetch_userinfo, enrich_user_from_userinfo

logger = logging.getLogger(__name__)

# Lazy import to avoid circular dependencies
_user_sync_service = None


def _get_user_sync_service():
    """Get UserSyncService instance, creating it lazily on first use."""
    global _user_sync_service
    if _user_sync_service is None:
        try:
            from apis.shared.users.repository import UserRepository
            from apis.shared.users.sync import UserSyncService
            repository = UserRepository()
            _user_sync_service = UserSyncService(repository=repository)
            if _user_sync_service.enabled:
                logger.info("UserSyncService initialized for JWT sync")
            else:
                logger.debug("UserSyncService disabled - no table configured")
        except Exception as e:
            logger.warning(f"Failed to initialize UserSyncService: {e}")
            _user_sync_service = None
    return _user_sync_service

# HTTP Bearer token security scheme with auto_error=False to handle missing tokens manually
security = HTTPBearer(auto_error=False)


async def _enrich_user_from_userinfo(user: User, provider, token: str) -> None:
    """Fetch missing profile claims from the provider's userinfo endpoint."""
    if (user.email and user.name) or not provider or not provider.userinfo_endpoint:
        return
    try:
        claims = await fetch_userinfo(
            userinfo_endpoint=provider.userinfo_endpoint,
            access_token=token,
            user_id=user.user_id,
        )
        if claims:
            enrich_user_from_userinfo(user, claims, provider)
    except Exception as e:
        logger.warning(f"Failed to enrich user {user.user_id} from userinfo: {e}")


async def _sync_user_background(sync_service, user: User) -> None:
    """Sync user to DynamoDB in the background (fire-and-forget)."""
    try:
        await sync_service.sync_user_from_jwt(user)
        logger.debug(f"Synced user {user.user_id} to Users table")
    except Exception as e:
        # Log but don't fail - sync should never break authentication
        logger.warning(f"Failed to sync user {user.user_id}: {e}")

# Lazy-initialized generic validator for multi-provider support
_generic_validator = None
_generic_validator_initialized = False


def _get_generic_validator():
    """
    Get the GenericOIDCJWTValidator instance.

    Returns None if the auth providers table is not configured.
    """
    global _generic_validator, _generic_validator_initialized
    if _generic_validator_initialized:
        return _generic_validator

    _generic_validator_initialized = True
    try:
        from apis.shared.auth_providers.repository import get_auth_provider_repository
        from .generic_jwt_validator import GenericOIDCJWTValidator

        repo = get_auth_provider_repository()
        if repo.enabled:
            _generic_validator = GenericOIDCJWTValidator(repo)
            logger.info("GenericOIDCJWTValidator initialized for multi-provider auth")
        else:
            logger.debug("Auth providers table not configured, generic validator disabled")
    except Exception as e:
        logger.debug(f"Generic validator not available: {e}")

    return _generic_validator


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> User:
    """
    FastAPI dependency to get the current authenticated user.

    Validates the JWT token using the GenericOIDCJWTValidator, which
    matches the token issuer to configured auth providers.

    Args:
        credentials: HTTP Bearer token credentials (None if missing)

    Returns:
        User object with authenticated user information

    Raises:
        HTTPException:
            - 401 if token is missing or invalid
            - 500 if no JWT validator is available
    """
    # Check if credentials are missing
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid Bearer token in the Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Use generic multi-provider validation
    generic_validator = _get_generic_validator()
    if generic_validator:
        try:
            provider = await generic_validator.resolve_provider_from_token(token)
            if provider:
                user = generic_validator.validate_token(token, provider)
                user.raw_token = token

                # Enrich missing profile claims from userinfo endpoint
                await _enrich_user_from_userinfo(user, provider, token)

                # Fire-and-forget sync to Users table
                sync_service = _get_user_sync_service()
                if sync_service and sync_service.enabled:
                    asyncio.create_task(_sync_user_background(sync_service, user))

                return user
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Token validation failed: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication failed."
            )

    # No validator available - no auth providers configured
    logger.error("No JWT validator available. Ensure at least one OIDC auth provider is configured.")
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Authentication service not configured. No OIDC auth providers have been set up."
    )


async def get_current_user_id(
    user: User = Depends(get_current_user)
) -> str:
    """
    FastAPI dependency to get the current user's ID as a string.

    This is a convenience wrapper around get_current_user that extracts
    just the user_id field. Useful when you only need the user ID and not
    the full User object.

    Args:
        user: User object from get_current_user dependency

    Returns:
        User ID string
    """
    return user.user_id


async def get_current_user_trusted(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> User:
    """
    FastAPI dependency to get current user from pre-validated JWT.

    Use this when JWT validation is already performed at the network level
    (e.g., by AWS Bedrock AgentCore Runtime's JWT authorizer). This method
    skips expensive signature verification and simply extracts claims from
    the token using the matching auth provider's claim mappings.

    Security: Only use this in services where the JWT validation
    is guaranteed. IE AgentCore Runtime with Inbound Auth. For services without pre-validation, use
    get_current_user() instead.

    Args:
        credentials: HTTP Bearer token credentials (None if missing)

    Returns:
        User object with authenticated user information

    Raises:
        HTTPException:
            - 401 if token is missing or malformed
    """
    logger.debug("[get_current_user_trusted] Trusted auth extraction started")

    # Check if credentials are missing
    if credentials is None:
        logger.debug("[get_current_user_trusted] No credentials provided - returning 401")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid Bearer token in the Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    try:
        # Decode JWT without verification (network layer already validated it)
        payload = jwt.decode(token, options={"verify_signature": False})
        logger.debug("[get_current_user_trusted] JWT decoded successfully")

        # Resolve provider for claim mappings
        generic_validator = _get_generic_validator()
        if generic_validator:
            try:
                provider = await generic_validator.resolve_provider_from_token(token)
                logger.debug(f"[get_current_user_trusted] Provider resolved: {provider.provider_id if provider else 'None'}")
                if provider:
                    # Use provider-specific claim extraction
                    # Fall back to common OIDC claims if primary claim is absent
                    email = (
                        payload.get(provider.email_claim)
                        or payload.get("preferred_username")
                        or payload.get("upn")
                    )
                    name = payload.get(provider.name_claim)
                    user_id = payload.get(provider.user_id_claim)
                    roles = payload.get(provider.roles_claim, [])
                    picture = payload.get(provider.picture_claim) if provider.picture_claim else None

                    logger.debug("[get_current_user_trusted] Claims extracted from token")

                    if not name and provider.first_name_claim and provider.last_name_claim:
                        first = payload.get(provider.first_name_claim, "")
                        last = payload.get(provider.last_name_claim, "")
                        name = f"{first} {last}".strip()

                    if isinstance(roles, str):
                        roles = [roles]

                    if not user_id:
                        logger.error("[get_current_user_trusted] Required user_id claim is missing/empty in token - returning 401")
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid user."
                        )

                    user = User(
                        email=str(email).lower() if email else "",
                        name=str(name) if name else "",
                        user_id=str(user_id),
                        roles=roles if isinstance(roles, list) else [],
                        picture=picture,
                        raw_token=token,
                    )

                    logger.debug("[get_current_user_trusted] User authenticated successfully via provider path")

                    # Enrich missing profile claims from userinfo endpoint
                    await _enrich_user_from_userinfo(user, provider, token)

                    sync_service = _get_user_sync_service()
                    if sync_service and sync_service.enabled:
                        asyncio.create_task(_sync_user_background(sync_service, user))

                    return user
                else:
                    logger.warning("[get_current_user_trusted] Provider resolved to None - falling through to generic extraction")
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"[get_current_user_trusted] Provider-based trusted extraction failed: {e}", exc_info=True)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication failed."
                )
        else:
            logger.warning("[get_current_user_trusted] No generic validator available (auth providers table not configured?)")

        # No auth providers configured - use standard OIDC claim extraction
        logger.warning("[get_current_user_trusted] Using standard OIDC claim fallback (no provider matched)")
        email = payload.get('email') or payload.get('preferred_username')
        name = payload.get('name') or (
            f"{payload.get('given_name', '')} {payload.get('family_name', '')}"
        ).strip()
        user_id = payload.get('sub')
        roles = payload.get('roles', [])
        picture = payload.get('picture')

        logger.debug("[get_current_user_trusted] Using OIDC fallback claim extraction")

        if not user_id:
            logger.error("[get_current_user_trusted] Missing 'sub' claim in token - returning 401")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user."
            )

        user = User(
            email=email.lower() if email else "",
            name=name,
            user_id=str(user_id),
            roles=roles,
            picture=picture,
            raw_token=token,
        )

        logger.debug("[get_current_user_trusted] User authenticated successfully via OIDC fallback")

        # Fire-and-forget sync to Users table
        sync_service = _get_user_sync_service()
        if sync_service and sync_service.enabled:
            asyncio.create_task(_sync_user_background(sync_service, user))

        return user

    except jwt.DecodeError as e:
        logger.error(f"[get_current_user_trusted] Failed to decode JWT token: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token."
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[get_current_user_trusted] Unexpected error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed."
        )
