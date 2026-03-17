"""Users API routes for non-admin user operations."""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List
import logging

from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User
from apis.shared.rbac.service import get_app_role_service
from apis.shared.users.repository import UserRepository
from apis.shared.users.models import UserProfile, UserListItem, UserStatus
from .models import UserMeResponse, UserSearchResult, UserSearchResponse, UserPermissionsResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


def get_user_repository() -> UserRepository:
    """Get user repository instance."""
    return UserRepository()


@router.get("/me", response_model=UserMeResponse)
async def get_me(
    current_user: User = Depends(get_current_user),
):
    """
    Return the authenticated user's profile.

    The User object is enriched by the auth dependency — if the JWT
    access token lacked profile claims (email, name, picture), they
    were fetched from the provider's OIDC userinfo endpoint.
    """
    return UserMeResponse(
        user_id=current_user.user_id,
        email=current_user.email,
        name=current_user.name,
        picture=current_user.picture,
    )


@router.get("/me/permissions", response_model=UserPermissionsResponse)
async def get_my_permissions(
    current_user: User = Depends(get_current_user),
):
    """
    Get the current user's effective permissions resolved from AppRoles.

    Any authenticated user can query their own permissions. Resolves JWT roles
    to AppRoles via the RBAC system and returns merged effective permissions.
    """
    logger.info(f"GET /users/me/permissions - User: {current_user.user_id}")
    try:
        service = get_app_role_service()
        permissions = await service.resolve_user_permissions(current_user)
        return UserPermissionsResponse(
            app_roles=permissions.app_roles,
            tools=permissions.tools,
            models=permissions.models,
            quota_tier=permissions.quota_tier,
            resolved_at=permissions.resolved_at,
        )
    except Exception as e:
        logger.error(f"Failed to resolve permissions for {current_user.user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to resolve user permissions"
        )


@router.get("/search", response_model=UserSearchResponse)
async def search_users(
    q: str = Query(..., description="Search query (email or name, partial match)"),
    limit: int = Query(20, ge=1, le=50, description="Maximum number of results"),
    current_user: User = Depends(get_current_user),
    user_repo: UserRepository = Depends(get_user_repository)
):
    """
    Search for users by email or name (partial match).
    
    This endpoint is used by the sharing modal to find existing users in the system.
    Only returns active users. Search matches against:
    - Email prefix (case-insensitive)
    - Name contains (case-insensitive)
    
    Requires JWT authentication. Available to all authenticated users (not admin-only).
    
    Args:
        q: Search query string
        limit: Maximum number of results (1-50, default 20)
        current_user: Authenticated user from JWT token (injected by dependency)
        user_repo: User repository instance (injected by dependency)
    
    Returns:
        UserSearchResponse with list of matching users
    
    Raises:
        HTTPException:
            - 401 if not authenticated
            - 500 if server error
    """
    logger.info(f"GET /users/search - User: {current_user.user_id}, Query: {q}, Limit: {limit}")
    
    if not user_repo.enabled:
        logger.debug("User repository not enabled - returning empty results")
        return UserSearchResponse(users=[])
    
    try:
        # Normalize search query
        query_lower = q.lower().strip()
        
        if not query_lower:
            return UserSearchResponse(users=[])
        
        # Search by email prefix using EmailIndex
        # Note: DynamoDB doesn't support contains queries efficiently, so we'll use prefix matching
        # For name matching, we'll need to scan/query and filter (less efficient but acceptable for small orgs)
        
        results = []
        seen_user_ids = set()
        
        # Try exact email match first (most common case)
        user_by_email = await user_repo.get_user_by_email(query_lower)
        if user_by_email and user_by_email.status == UserStatus.ACTIVE:
            if user_by_email.user_id not in seen_user_ids:
                results.append(UserSearchResult(
                    user_id=user_by_email.user_id,
                    email=user_by_email.email,
                    name=user_by_email.name
                ))
                seen_user_ids.add(user_by_email.user_id)
        
        # If we already have enough results from exact match, return early
        if len(results) >= limit:
            return UserSearchResponse(users=results[:limit])
        
        # Search by email prefix (if query looks like email start)
        # Note: DynamoDB EmailIndex doesn't support begins_with efficiently without a scan
        # For now, we'll use a simplified approach: if query contains @, try exact match
        # For broader search, we'd need to implement a scan with filter (acceptable for small user bases)
        
        # Search by name contains (scan active users)
        # This is less efficient but necessary for name-based search
        # We'll limit to first 100 active users and filter by name
        active_users, _ = await user_repo.list_users_by_status(
            status=UserStatus.ACTIVE.value,
            limit=100,  # Scan up to 100 active users
            last_evaluated_key=None
        )
        
        # Filter by name contains (case-insensitive)
        for user in active_users:
            if user.user_id in seen_user_ids:
                continue
            
            # Check if name contains query (case-insensitive)
            if query_lower in user.name.lower():
                results.append(UserSearchResult(
                    user_id=user.user_id,
                    email=user.email,
                    name=user.name
                ))
                seen_user_ids.add(user.user_id)
            
            # Also check if email contains query (for partial email matches)
            elif query_lower in user.email.lower():
                results.append(UserSearchResult(
                    user_id=user.user_id,
                    email=user.email,
                    name=user.name
                ))
                seen_user_ids.add(user.user_id)
            
            # Stop if we have enough results
            if len(results) >= limit:
                break
        
        # Sort results: exact email matches first, then by name
        results.sort(key=lambda x: (
            0 if query_lower == x.email.lower() else 1,  # Exact email match first
            x.name.lower()  # Then by name
        ))
        
        # Limit results
        results = results[:limit]
        
        logger.info(f"Found {len(results)} users matching query '{q}'")
        return UserSearchResponse(users=results)
    
    except Exception as e:
        logger.error(f"Error searching users: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to search users: {str(e)}"
        )
