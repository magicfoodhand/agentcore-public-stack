"""User search models for sharing functionality."""

from typing import List, Optional
from pydantic import BaseModel, Field, ConfigDict


class UserSearchResult(BaseModel):
    """User search result for sharing modal."""
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId", description="User identifier")
    email: str = Field(..., description="User email address")
    name: str = Field(..., description="User display name")


class UserSearchResponse(BaseModel):
    """Response containing user search results."""
    model_config = ConfigDict(populate_by_name=True)

    users: List[UserSearchResult] = Field(..., description="List of matching users")


class UserMeResponse(BaseModel):
    """Response model for GET /users/me — the authenticated user's profile."""
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId", description="User identifier")
    email: str = Field(..., description="User email address")
    name: str = Field(..., description="User display name")
    picture: Optional[str] = Field(None, description="Profile picture URL")


class UserPermissionsResponse(BaseModel):
    """Response model for user effective permissions resolved from AppRoles."""
    model_config = ConfigDict(populate_by_name=True)

    app_roles: List[str] = Field(..., alias="appRoles", description="Resolved application roles")
    tools: List[str] = Field(..., description="Accessible tool IDs")
    models: List[str] = Field(..., description="Accessible model IDs")
    quota_tier: Optional[str] = Field(None, alias="quotaTier", description="Assigned quota tier")
    resolved_at: str = Field(..., alias="resolvedAt", description="ISO timestamp of resolution")
