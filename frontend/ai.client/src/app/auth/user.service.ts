import { inject, Injectable, signal, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { AuthService } from './auth.service';
import { ConfigService } from '../services/config.service';
import { User, JWTPayload, UserPermissions } from './user.model';

/**
 * Service for managing current user information decoded from JWT tokens.
 * Automatically syncs with AuthService to update user data when tokens change.
 */
@Injectable({
  providedIn: 'root'
})
export class UserService {
  private authService = inject(AuthService);
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  /**
   * Current user signal. Null if not authenticated or token cannot be decoded.
   */
  readonly currentUser = signal<User | null>(null);

  /** Application roles resolved from the backend RBAC system. */
  readonly appRoles = signal<string[]>([]);

  /** Whether the current user has the system_admin AppRole. */
  readonly isAdmin = computed(() => this.appRoles().includes('system_admin'));

  /** Promise for the in-flight permissions fetch, used to avoid duplicate requests. */
  private _permissionsPromise: Promise<void> | null = null;

  constructor() {
    // Initialize user from current token or set anonymous user if auth disabled
    this.refreshUser();

    // If user has a token on page load, fetch permissions from backend
    if (this.authService.getAccessToken()) {
      this._permissionsPromise = this.fetchPermissions();
    }

    if (typeof window !== 'undefined') {
      // Listen for storage events to sync when tokens change in other tabs/windows
      window.addEventListener('storage', (event) => {
        if (event.key === 'access_token' || event.key === null) {
          this.refreshUser();
        }
      });

      // Listen for custom events to sync when tokens change in same tab
      window.addEventListener('token-stored', () => {
        this.refreshUser();
        this._permissionsPromise = this.fetchPermissions();
      });

      window.addEventListener('token-cleared', () => {
        this.currentUser.set(null);
        this.appRoles.set([]);
        this._permissionsPromise = null;
      });
    }
  }

  /**
   * Decode and update user information from JWT token.
   * If the token lacks profile claims (email, name), fetches
   * the enriched profile from the backend /users/me endpoint.
   * @param token JWT access token
   */
  private updateUserFromToken(token: string): void {
    try {
      const user = this.decodeToken(token);
      this.currentUser.set(user);

      // If profile claims are missing from the token, fetch from backend
      if (!user.email || !user.fullName) {
        this.fetchUserProfile();
      }
    } catch (error) {
      console.error('Failed to decode user from token:', error);
      this.currentUser.set(null);
    }
  }

  /**
   * Decode JWT token and extract user information.
   * @param token JWT access token
   * @returns User object with decoded information
   * @throws Error if token is invalid or missing required claims
   */
  private decodeToken(token: string): User {
    try {
      // JWT tokens have three parts: header.payload.signature
      const parts = token.split('.');
      if (parts.length !== 3) {
        throw new Error('Invalid token format');
      }

      // Decode the payload (middle part)
      const payload = this.base64UrlDecode(parts[1]);
      const jwtPayload: JWTPayload = JSON.parse(payload);
      
      // Extract user identity from standard OIDC claims
      const email = jwtPayload.email || jwtPayload.preferred_username || '';
      const userId = jwtPayload.sub || '';

      if (!userId && !email) {
        throw new Error('Token missing both sub and email claims');
      }

      // Build full name from available claims
      const fullName = jwtPayload.name ||
        `${jwtPayload.given_name || ''} ${jwtPayload.family_name || ''}`.trim() ||
        email;

      // Extract first and last name - prefer JWT claims, fall back to parsing fullName
      let firstName = jwtPayload.given_name || '';
      let lastName = jwtPayload.family_name || '';

      // If given_name/family_name not available, parse from fullName
      if (!firstName && fullName) {
        const nameParts = fullName.split(' ');
        firstName = nameParts[0] || '';
        lastName = nameParts.slice(1).join(' ') || '';
      }

      const roles = jwtPayload.roles || [];

      const user: User = {
        email,
        user_id: userId || email,
        firstName,
        lastName,
        fullName,
        roles,
        picture: jwtPayload.picture
      };

      return user;
    } catch (error) {
      if (error instanceof Error) {
        throw new Error(`Failed to decode token: ${error.message}`);
      }
      throw new Error('Failed to decode token: Unknown error');
    }
  }

  /**
   * Decode base64url encoded string.
   * @param str Base64url encoded string
   * @returns Decoded string
   */
  private base64UrlDecode(str: string): string {
    // Convert base64url to base64
    let base64 = str.replace(/-/g, '+').replace(/_/g, '/');
    
    // Add padding if needed
    while (base64.length % 4) {
      base64 += '=';
    }

    // Decode base64
    try {
      const decoded = atob(base64);
      return decoded;
    } catch (error) {
      throw new Error('Invalid base64 encoding');
    }
  }

  /**
   * Get the current user synchronously.
   * @returns Current user or null if not authenticated
   */
  getUser(): User | null {
    return this.currentUser();
  }

  /**
   * Check if user has a specific role.
   * @param role Role to check
   * @returns True if user has the role
   */
  hasRole(role: string): boolean {
    const user = this.currentUser();
    return user?.roles.includes(role) ?? false;
  }

  /**
   * Check if user has any of the specified roles.
   * @param roles Roles to check
   * @returns True if user has at least one of the roles
   */
  hasAnyRole(roles: string[]): boolean {
    const user = this.currentUser();
    if (!user) return false;
    return roles.some(role => user.roles.includes(role));
  }

  /**
   * Fetch the enriched user profile from the backend.
   * Called when the JWT access token lacks profile claims (email, name).
   * The backend enriches the user via the OIDC userinfo endpoint.
   */
  private async fetchUserProfile(): Promise<void> {
    try {
      const url = `${this.config.appApiUrl()}/users/me`;
      const profile = await firstValueFrom(
        this.http.get<{ userId: string; email: string; name: string; picture?: string }>(url)
      );

      const current = this.currentUser();
      if (!current) return;

      // Parse name into first/last
      const nameParts = (profile.name || '').split(' ');
      const firstName = nameParts[0] || current.firstName;
      const lastName = nameParts.slice(1).join(' ') || current.lastName;
      const fullName = profile.name || current.fullName;

      this.currentUser.set({
        ...current,
        email: profile.email || current.email,
        firstName,
        lastName,
        fullName,
        picture: profile.picture || current.picture,
      });
    } catch (error) {
      console.error('Failed to fetch user profile from backend:', error);
    }
  }

  /**
   * Fetch permissions from the backend RBAC system.
   * Updates the appRoles signal with resolved application roles.
   */
  async fetchPermissions(): Promise<void> {
    try {
      const url = `${this.config.appApiUrl()}/users/me/permissions`;
      const permissions = await firstValueFrom(
        this.http.get<UserPermissions>(url)
      );
      this.appRoles.set(permissions.appRoles);
    } catch (error) {
      console.error('Failed to fetch user permissions:', error);
      this.appRoles.set([]);
    }
  }

  /**
   * Ensure permissions have been loaded. Awaits any in-flight fetch
   * or starts a new one if needed. Used by guards to handle direct navigation.
   */
  async ensurePermissionsLoaded(): Promise<void> {
    if (this._permissionsPromise) {
      await this._permissionsPromise;
    } else if (this.authService.getAccessToken()) {
      this._permissionsPromise = this.fetchPermissions();
      await this._permissionsPromise;
    }
  }

  /**
   * Check if user has a specific AppRole.
   * @param role AppRole to check (e.g., 'system_admin')
   */
  hasAppRole(role: string): boolean {
    return this.appRoles().includes(role);
  }

  /**
   * Manually refresh user data from current token.
   * Useful when token has been updated externally.
   */
  refreshUser(): void {
    const token = this.authService.getAccessToken();
    if (token) {
      this.updateUserFromToken(token);
    } else {
      this.currentUser.set(null);
    }
  }
}

