import { inject } from '@angular/core';
import { Router, CanActivateFn } from '@angular/router';
import { AuthService } from './auth.service';
import { UserService } from './user.service';

/**
 * Route guard that protects admin routes using backend RBAC resolution.
 *
 * Checks if the user is authenticated and has the `system_admin` AppRole
 * (resolved from the backend RBAC system, not raw JWT roles).
 *
 * If not authenticated, redirects to /auth/login.
 * If authenticated but lacks system_admin AppRole, redirects to home page.
 *
 * @returns True if user is authenticated and has system_admin AppRole, false otherwise
 */
export const adminGuard: CanActivateFn = async (route, state) => {
  const authService = inject(AuthService);
  const userService = inject(UserService);
  const router = inject(Router);

  // Check if user is authenticated
  if (!authService.isAuthenticated()) {
    // If not authenticated, try to refresh token if expired
    const token = authService.getAccessToken();
    if (token && authService.isTokenExpired()) {
      try {
        await authService.refreshAccessToken();
        userService.refreshUser();
      } catch (error) {
        // Refresh failed, redirect to login
        router.navigate(['/auth/login'], {
          queryParams: { returnUrl: state.url }
        });
        return false;
      }
    } else {
      // No token or refresh failed, redirect to login
      router.navigate(['/auth/login'], {
        queryParams: { returnUrl: state.url }
      });
      return false;
    }
  }

  // Ensure permissions are resolved from backend RBAC before checking
  await userService.ensurePermissionsLoaded();

  // Check for system_admin AppRole (resolved from backend RBAC, not raw JWT roles)
  if (!userService.isAdmin()) {
    console.warn('User lacks system_admin AppRole:', userService.getUser()?.roles);
    router.navigate(['/']);
    return false;
  }

  return true;
};

/**
 * ⚠ ⚠️  IMPORTANT: Update Google API credentials before using search tools
[INFO] 
[INFO] The secret was created with placeholder values. Update with real credentials:
[INFO] 
[INFO]   aws secretsmanager put-secret-value \
[INFO]     --secret-id ai-inapinch-io/mcp/google-credentials \
[INFO]     --secret-string '{"api_key":"YOUR_API_KEY","search_engine_id":"YOUR_ENGINE_ID"}' \
[INFO]     --region us-west-2
[INFO] 
[INFO] Get credentials from:
[INFO]   - API Key: https://console.cloud.google.com/apis/credentials
[INFO]   - Search Engine ID: https://programmablesearchengine.google.com/
[INFO] 
[INFO] ============================================================
[INFO] 
[INFO] 1. Test Gateway connectivity:
[INFO]    aws bedrock-agentcore list-gateway-targets \
[INFO]      --gateway-identifier ${GATEWAY_ID} \
[INFO]      --region us-west-2
[INFO] 
[INFO] 2. View Gateway details in AWS Console:
[INFO]    https://console.aws.amazon.com/bedrock/home?region=us-west-2#/agentcore/gateways
[INFO] 
[INFO] 3. Integrate with AgentCore Runtime:
[INFO]    - Update Runtime environment with Gateway URL from SSM
[INFO]    - Ensure Runtime execution role has bedrock-agentcore:InvokeGateway permission
[INFO] 
 */