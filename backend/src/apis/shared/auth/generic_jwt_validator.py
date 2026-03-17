"""Generic OIDC JWT validator that works with any configured auth provider."""

import logging
import re
from typing import Dict, Optional

import jwt
from jwt import PyJWKClient
from fastapi import HTTPException, status

from .models import User
from apis.shared.auth_providers.models import AuthProvider
from apis.shared.auth_providers.repository import AuthProviderRepository

logger = logging.getLogger(__name__)


class GenericOIDCJWTValidator:
    """
    Validates JWT tokens against dynamically configured OIDC providers.

    Resolves the provider from the token's issuer claim, then validates
    the token using that provider's JWKS and claim mappings.
    """

    def __init__(self, provider_repo: AuthProviderRepository):
        self._provider_repo = provider_repo
        # Cache PyJWKClient instances per provider (keyed by jwks_uri)
        self._jwks_clients: Dict[str, PyJWKClient] = {}
        # Cache issuer -> provider mapping for fast lookups
        self._issuer_to_provider: Dict[str, AuthProvider] = {}

    def _get_jwks_client(self, provider: AuthProvider) -> PyJWKClient:
        """Get or create a cached PyJWKClient for the provider's JWKS URI."""
        jwks_uri = provider.jwks_uri
        if not jwks_uri:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Auth provider '{provider.provider_id}' has no JWKS URI configured",
            )

        if jwks_uri not in self._jwks_clients:
            self._jwks_clients[jwks_uri] = PyJWKClient(
                jwks_uri,
                cache_keys=True,
                max_cached_keys=5,
            )

        return self._jwks_clients[jwks_uri]

    async def resolve_provider_from_token(self, token: str) -> Optional[AuthProvider]:
        """
        Resolve which auth provider issued a token by matching the issuer claim.

        Args:
            token: JWT token string

        Returns:
            AuthProvider if a matching enabled provider is found, None otherwise
        """
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
            issuer = unverified.get("iss")
            if not issuer:
                return None

            # Check cache first
            if issuer in self._issuer_to_provider:
                cached = self._issuer_to_provider[issuer]
                if cached.enabled:
                    return cached

            # Query enabled providers and match by issuer
            providers = await self._provider_repo.list_providers(enabled_only=True)
            for provider in providers:
                if self._issuer_matches(issuer, provider):
                    self._issuer_to_provider[issuer] = provider
                    return provider

            return None

        except jwt.DecodeError:
            return None
        except Exception as e:
            logger.debug(f"Error resolving provider from token: {e}")
            return None

    def invalidate_cache(self) -> None:
        """Clear all cached data. Call when providers are updated."""
        self._issuer_to_provider.clear()
        self._jwks_clients.clear()

    def validate_token(self, token: str, provider: AuthProvider) -> User:
        """
        Validate a JWT token using the provider's configuration and extract user info.

        Args:
            token: JWT token string
            provider: The AuthProvider configuration to validate against

        Returns:
            User object with extracted claims

        Raises:
            HTTPException: If validation fails
        """
        try:
            # Log token details for debugging (only when DEBUG is enabled)
            if logger.isEnabledFor(logging.DEBUG):
                try:
                    token_header = jwt.get_unverified_header(token)
                    logger.debug(
                        f"Validating token for provider {provider.provider_id}: "
                        f"alg={token_header.get('alg')}"
                    )
                except Exception:
                    logger.debug("Could not decode token header for inspection")

            # Get signing key from provider's JWKS
            jwks_client = self._get_jwks_client(provider)
            signing_key = jwks_client.get_signing_key_from_jwt(token)

            # Decode and validate token
            # Allow issuer mismatch for providers like Entra ID where
            # the token issuer (v1: sts.windows.net) differs from the
            # OIDC discovery issuer (v2: login.microsoftonline.com)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
                options={
                    "verify_signature": True,
                    "verify_aud": False,  # Manual audience verification below
                    "verify_iss": False,  # Manual issuer verification below
                    "verify_exp": True,
                },
                leeway=60,
            )

            # Manual issuer verification: accept both the configured issuer
            # and known variant issuers (e.g., Entra ID v1 vs v2)
            token_issuer = payload.get("iss", "")
            if not self._issuer_matches(token_issuer, provider):
                logger.warning(
                    f"Token issuer '{token_issuer}' does not match provider "
                    f"'{provider.provider_id}' issuer '{provider.issuer_url}'"
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Invalid token issuer.",
                )

            # Verify audience if configured
            if provider.allowed_audiences:
                token_aud = payload.get("aud")
                if isinstance(token_aud, str):
                    token_audiences = [token_aud]
                elif isinstance(token_aud, list):
                    token_audiences = token_aud
                else:
                    token_audiences = []

                if not any(aud in provider.allowed_audiences for aud in token_audiences):
                    logger.warning(
                        f"Token audience {token_aud} not in allowed audiences "
                        f"for provider {provider.provider_id}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid token audience.",
                    )

            # Verify required scopes if configured
            if provider.required_scopes:
                scp_claim = payload.get("scp", "")
                token_scopes = scp_claim.split() if scp_claim else []
                for required_scope in provider.required_scopes:
                    if required_scope not in token_scopes:
                        logger.warning(
                            f"Token missing required scope '{required_scope}' "
                            f"for provider {provider.provider_id}"
                        )
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=f"Token missing required scope: {required_scope}",
                        )

            # Extract user info using provider's claim mappings
            user_id = self._extract_claim(payload, provider.user_id_claim)
            email = (
                self._extract_claim(payload, provider.email_claim)
                or payload.get("preferred_username")
                or payload.get("upn")
            )
            name = self._extract_claim(payload, provider.name_claim)
            roles = payload.get(provider.roles_claim, [])
            picture = payload.get(provider.picture_claim) if provider.picture_claim else None

            # Build full name from first/last if name claim is empty
            if not name and provider.first_name_claim and provider.last_name_claim:
                first = payload.get(provider.first_name_claim, "")
                last = payload.get(provider.last_name_claim, "")
                name = f"{first} {last}".strip()

            # Ensure roles is a list
            if isinstance(roles, str):
                roles = [roles]
            elif not isinstance(roles, list):
                roles = []

            # Validate user_id format if pattern is configured
            if provider.user_id_pattern and user_id:
                user_id_str = str(user_id)
                if not re.match(provider.user_id_pattern, user_id_str):
                    logger.warning(
                        f"User ID '{user_id_str}' does not match pattern "
                        f"'{provider.user_id_pattern}' for provider {provider.provider_id}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid user.",
                    )

            if not user_id:
                logger.warning(
                    f"Missing user_id claim '{provider.user_id_claim}' "
                    f"for provider {provider.provider_id}"
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid user.",
                )

            return User(
                email=str(email).lower() if email else "",
                user_id=str(user_id),
                name=str(name) if name else (str(email) if email else ""),
                roles=roles,
                picture=picture,
            )

        except jwt.InvalidSignatureError as e:
            logger.error(f"Invalid token signature for provider {provider.provider_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token signature.",
            )
        except jwt.InvalidIssuerError as e:
            logger.error(f"Invalid issuer for provider {provider.provider_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token issuer. Expected: {provider.issuer_url}",
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired. Please refresh your session.",
            )
        except jwt.InvalidTokenError as e:
            logger.error(f"Invalid token for provider {provider.provider_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token.",
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                f"Error validating token for provider {provider.provider_id}: {e}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token validation failed.",
            )

    def _issuer_matches(self, token_issuer: str, provider: AuthProvider) -> bool:
        """
        Check if a token's issuer matches a provider, accounting for known
        issuer variants (e.g., Entra ID v1 vs v2 endpoints).

        Entra ID v2 issuer: https://login.microsoftonline.com/{tenant}/v2.0
        Entra ID v1 issuer: https://sts.windows.net/{tenant}/
        Both are valid for the same tenant.
        """
        provider_issuer = provider.issuer_url.rstrip("/")
        token_iss = token_issuer.rstrip("/")

        # Direct match
        if provider_issuer == token_iss:
            return True

        # Entra ID v1/v2 cross-match
        # Extract tenant ID from either format and compare
        v2_pattern = r"https://login\.microsoftonline\.com/([^/]+)/v2\.0"
        v1_pattern = r"https://sts\.windows\.net/([^/]+)"

        v2_match_provider = re.match(v2_pattern, provider_issuer)
        v1_match_token = re.match(v1_pattern, token_iss)
        if v2_match_provider and v1_match_token:
            if v2_match_provider.group(1) == v1_match_token.group(1):
                return True

        v1_match_provider = re.match(v1_pattern, provider_issuer)
        v2_match_token = re.match(v2_pattern, token_iss)
        if v1_match_provider and v2_match_token:
            if v1_match_provider.group(1) == v2_match_token.group(1):
                return True

        return False

    def _extract_claim(self, payload: dict, claim_path: str) -> Optional[str]:
        """
        Extract a claim value from the JWT payload.

        Supports nested claims using dot notation or URI-based claims
        like 'http://schemas.example.com/claims/id'.
        """
        if not claim_path:
            return None

        # Direct lookup first (handles URI-style claims)
        value = payload.get(claim_path)
        if value is not None:
            return value

        # Try dot-notation for nested claims (e.g., "address.country")
        if "." in claim_path and not claim_path.startswith("http"):
            parts = claim_path.split(".")
            current = payload
            for part in parts:
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    return None
            return current

        return None
