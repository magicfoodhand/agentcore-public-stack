import { Component, signal, ChangeDetectionStrategy, inject, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { ActivatedRoute } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { AuthService } from '../auth.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { ConfigService } from '../../services/config.service';

interface AuthProviderPublicInfo {
  provider_id: string;
  display_name: string;
  logo_url?: string;
  button_color?: string;
}

interface AuthProviderPublicListResponse {
  providers: AuthProviderPublicInfo[];
}

@Component({
  selector: 'app-login',
  imports: [CommonModule],
  styleUrl: './login.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="fixed inset-0 flex items-center justify-center bg-gray-50 dark:bg-gray-900 overflow-y-auto">
      <!-- Sign-in card uses narrow width; setup guide uses wider width -->
      <div class="w-full px-4 py-12"
        [class.max-w-md]="!providersLoading() && providers().length > 0"
        [class.max-w-xl]="providersLoading() || providers().length === 0">
        <!-- Logo -->
        <div class="mb-8 flex justify-center">
          <img
            src="/img/logo-light.svg"
            alt="In a Pinch Logo"
            class="size-16 dark:hidden">
          <img
            src="/img/logo-dark.svg"
            alt="In a Pinch Logo"
            class="hidden size-16 dark:block">
        </div>

        <!-- Sign In Card (when providers exist) -->
        @if (!providersLoading() && providers().length > 0) {
          <div class="bg-white dark:bg-gray-800 rounded-lg shadow-sm p-8">
            <div class="flex flex-col items-center gap-6">
              <div class="flex flex-col items-center gap-2">
                <h1 class="text-2xl font-semibold text-gray-900 dark:text-gray-100">
                  Sign In
                </h1>
                <p class="text-base/7 text-gray-600 dark:text-gray-400 text-center">
                  @if (providers().length > 1) {
                    Choose an authentication provider to continue
                  } @else {
                    Sign in to continue
                  }
                </p>
              </div>

              @if (errorMessage()) {
                <div class="w-full p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg" role="alert">
                  <div class="flex items-start gap-3">
                    <svg class="size-5 text-red-600 dark:text-red-400 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    <p class="text-sm text-red-800 dark:text-red-300">
                      {{ errorMessage() }}
                    </p>
                  </div>
                </div>
              }

              <div class="w-full flex flex-col gap-3">
                @for (provider of providers(); track provider.provider_id) {
                  <button
                    type="button"
                    (click)="handleProviderLogin(provider)"
                    [disabled]="isLoading()"
                    class="w-full px-4 py-3 text-white font-medium rounded-lg transition-all duration-200 flex items-center justify-center gap-3 disabled:opacity-60"
                    [style.background-color]="provider.button_color || '#2563eb'"
                    [style.--hover-bg]="provider.button_color ? adjustBrightness(provider.button_color, -15) : '#1d4ed8'"
                  >
                    @if (isLoading() && activeProviderId() === provider.provider_id) {
                      <div class="size-5 border-2 border-white border-t-transparent rounded-full animate-spin"></div>
                      <span>Connecting...</span>
                    } @else {
                      @if (provider.logo_url) {
                        <img [src]="provider.logo_url" [alt]="provider.display_name" class="size-5 object-contain" />
                      } @else {
                        <svg class="size-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
                        </svg>
                      }
                      <span>Sign in with {{ provider.display_name }}</span>
                    }
                  </button>
                }
              </div>

              <p class="text-xs text-gray-500 dark:text-gray-400 text-center">
                You will be redirected to your identity provider to complete authentication
              </p>
            </div>
          </div>
        }

        <!-- Loading state -->
        @if (providersLoading()) {
          <div class="bg-white dark:bg-gray-800 rounded-lg shadow-sm p-8">
            <div class="flex justify-center py-4">
              <div class="size-6 border-2 border-gray-300 dark:border-gray-600 border-t-blue-600 dark:border-t-blue-400 rounded-full animate-spin" role="status">
                <span class="sr-only">Loading authentication providers</span>
              </div>
            </div>
          </div>
        }

        <!-- No Providers Setup Guide -->
        @if (!providersLoading() && providers().length === 0) {
          <div class="bg-white dark:bg-gray-800 rounded-lg shadow-sm p-8">
            <div class="flex flex-col gap-5">
              <!-- Header -->
              <div class="flex flex-col items-center gap-3 text-center">
                <div class="flex items-center justify-center size-12 rounded-full bg-amber-100 dark:bg-amber-900/30">
                  <svg class="size-6 text-amber-600 dark:text-amber-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4.5c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z" />
                  </svg>
                </div>
                <h1 class="text-xl font-semibold text-gray-900 dark:text-gray-100">
                  Authentication Not Configured
                </h1>
                <p class="text-sm text-gray-600 dark:text-gray-400">
                  No OIDC authentication providers have been set up yet. An administrator needs to seed an initial provider before users can sign in.
                </p>
              </div>

              <hr class="border-gray-200 dark:border-gray-700" />

              <!-- Setup Steps -->
              <div class="flex flex-col gap-4">
                <h2 class="text-sm font-semibold text-gray-900 dark:text-gray-100 uppercase tracking-wide">
                  Setup Instructions
                </h2>

                <!-- Step 1 -->
                <div class="flex gap-3">
                  <span class="flex items-center justify-center size-6 shrink-0 rounded-full bg-blue-100 dark:bg-blue-900/30 text-xs font-bold text-blue-700 dark:text-blue-300" aria-hidden="true">1</span>
                  <div class="flex flex-col gap-1">
                    <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Register an OIDC application with your Identity Provider</p>
                    <p class="text-xs text-gray-500 dark:text-gray-400">
                      Create an app registration in your IdP (e.g., Entra ID, Okta, Auth0, AWS Cognito). You will need:
                    </p>
                    <ul class="mt-1 flex flex-col gap-2 text-xs text-gray-600 dark:text-gray-400 list-disc list-inside">
                      <li>
                        <span class="font-medium text-gray-800 dark:text-gray-200">Issuer URL</span> &mdash; the OIDC issuer for your IdP
                        <ul class="mt-1 ml-4 flex flex-col gap-0.5 list-none text-gray-500 dark:text-gray-400">
                          <li>Entra ID: <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs">https://login.microsoftonline.com/&#123;tenant-id&#125;/v2.0</code></li>
                          <li>Cognito: <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs">https://cognito-idp.&#123;region&#125;.amazonaws.com/&#123;user-pool-id&#125;</code></li>
                          <li>Okta: <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs">https://&#123;domain&#125;.okta.com/oauth2/default</code></li>
                        </ul>
                      </li>
                      <li><span class="font-medium text-gray-800 dark:text-gray-200">Client ID</span> &mdash; the OIDC application/client identifier</li>
                      <li><span class="font-medium text-gray-800 dark:text-gray-200">Client Secret</span> &mdash; the OIDC client secret</li>
                    </ul>
                  </div>
                </div>

                <!-- Step 2 -->
                <div class="flex gap-3">
                  <span class="flex items-center justify-center size-6 shrink-0 rounded-full bg-blue-100 dark:bg-blue-900/30 text-xs font-bold text-blue-700 dark:text-blue-300" aria-hidden="true">2</span>
                  <div class="flex flex-col gap-1">
                    <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Ensure AWS resources are deployed</p>
                    <p class="text-xs text-gray-500 dark:text-gray-400">
                      The CDK <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs text-xs">AppApiStack</code> creates the DynamoDB auth providers table and Secrets Manager secret. You can find these values in the CDK stack outputs or in the AWS Console:
                    </p>
                    <ul class="mt-1 flex flex-col gap-1 text-xs text-gray-600 dark:text-gray-400 list-disc list-inside">
                      <li><span class="font-medium text-gray-800 dark:text-gray-200">DynamoDB Table Name</span> &mdash; the auth-providers table name</li>
                      <li><span class="font-medium text-gray-800 dark:text-gray-200">Secrets Manager ARN</span> &mdash; the secret ARN for provider client secrets</li>
                    </ul>
                  </div>
                </div>

                <!-- Step 3 -->
                <div class="flex gap-3">
                  <span class="flex items-center justify-center size-6 shrink-0 rounded-full bg-blue-100 dark:bg-blue-900/30 text-xs font-bold text-blue-700 dark:text-blue-300" aria-hidden="true">3</span>
                  <div class="flex flex-col gap-1">
                    <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Run the seed script</p>
                    <p class="text-xs text-gray-500 dark:text-gray-400">
                      From the project root, run:
                    </p>
                    <div class="mt-1.5 p-3 bg-gray-900 dark:bg-gray-950 rounded-sm">
                      <pre class="text-xs text-green-400 font-mono whitespace-pre-wrap break-all leading-relaxed">python backend/scripts/seed_auth_provider.py \
  --provider-id my-provider \
  --display-name "My Provider" \
  --issuer-url "https://..." \
  --client-id "your-client-id" \
  --client-secret "your-secret" \
  --discover \
  --table-name auth-providers \
  --secrets-arn "arn:aws:secretsmanager:..."</pre>
                    </div>
                    <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                      Use <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs text-xs">--discover</code> to auto-detect OIDC endpoints from the issuer URL. Run with no flags for interactive mode.
                    </p>
                  </div>
                </div>

                <!-- Step 4 -->
                <div class="flex gap-3">
                  <span class="flex items-center justify-center size-6 shrink-0 rounded-full bg-blue-100 dark:bg-blue-900/30 text-xs font-bold text-blue-700 dark:text-blue-300" aria-hidden="true">4</span>
                  <div class="flex flex-col gap-1">
                    <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Configure environment &amp; restart</p>
                    <p class="text-xs text-gray-500 dark:text-gray-400">
                      The following environment variables must be available to the backend service. For deployed environments, these are set automatically via CDK and SSM parameters. For local development, add them to your <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs text-xs">.env</code> file:
                    </p>
                    <ul class="mt-1 flex flex-col gap-2 text-xs text-gray-600 dark:text-gray-400 list-disc list-inside">
                      <li>
                        <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs text-xs">DYNAMODB_AUTH_PROVIDERS_TABLE_NAME</code>
                        <span class="text-gray-500 dark:text-gray-400"> &mdash; set via CDK/SSM on deploy; only needed in <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs text-xs">.env</code> for local dev</span>
                      </li>
                      <li>
                        <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs text-xs">AUTH_PROVIDER_SECRETS_ARN</code>
                        <span class="text-gray-500 dark:text-gray-400"> &mdash; set via CDK/SSM on deploy; only needed in <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs text-xs">.env</code> for local dev</span>
                      </li>
                      <li>
                        <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs text-xs">SEED_ADMIN_JWT_ROLE=YourAdminRole</code>
                        <span class="text-gray-500 dark:text-gray-400"> &mdash; set via the bootstrap seed script to map a JWT role to the system_admin AppRole. Must match a role your IdP issues in the token&rsquo;s <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs text-xs">roles</code> claim. The first user who logs in with this role can then manage providers, models, and roles from the admin dashboard.</span>
                      </li>
                    </ul>
                    <p class="mt-1.5 text-xs text-gray-500 dark:text-gray-400">
                      After configuring, restart the backend service and refresh this page.
                    </p>
                  </div>
                </div>
              </div>

              <hr class="border-gray-200 dark:border-gray-700" />

              <!-- Seed script options -->
              <details class="group">
                <summary class="flex items-center gap-2 cursor-pointer text-sm font-medium text-gray-700 dark:text-gray-300 select-none">
                  <svg class="size-4 text-gray-400 transition-transform group-open:rotate-90" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                  </svg>
                  Optional seed script flags
                </summary>
                <div class="mt-3 ml-6 flex flex-col gap-2 text-xs text-gray-600 dark:text-gray-400">
                  <div class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5">
                    <code class="font-mono text-gray-800 dark:text-gray-200">--scopes</code>
                    <span>OIDC scopes (default: <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs">openid profile email</code>)</span>
                    <code class="font-mono text-gray-800 dark:text-gray-200">--pkce-enabled</code>
                    <span>Enable PKCE (default: true)</span>
                    <code class="font-mono text-gray-800 dark:text-gray-200">--redirect-uri</code>
                    <span>Override redirect URI</span>
                    <code class="font-mono text-gray-800 dark:text-gray-200">--user-id-claim</code>
                    <span>JWT claim for user ID (default: <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs">sub</code>)</span>
                    <code class="font-mono text-gray-800 dark:text-gray-200">--roles-claim</code>
                    <span>JWT claim for roles (default: <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs">roles</code>)</span>
                    <code class="font-mono text-gray-800 dark:text-gray-200">--button-color</code>
                    <span>Hex color for login button (e.g., <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-xs">#0078D4</code>)</span>
                    <code class="font-mono text-gray-800 dark:text-gray-200">--logo-url</code>
                    <span>URL to provider logo for login button</span>
                    <code class="font-mono text-gray-800 dark:text-gray-200">--dry-run</code>
                    <span>Preview changes without writing to AWS</span>
                  </div>
                </div>
              </details>
            </div>
          </div>
        }
      </div>
    </div>
  `
})
export class LoginPage implements OnInit, OnDestroy {
  private authService = inject(AuthService);
  private sidenavService = inject(SidenavService);
  private config = inject(ConfigService);
  private http = inject(HttpClient);
  private route = inject(ActivatedRoute);

  isLoading = signal(false);
  errorMessage = signal<string | null>(null);
  providers = signal<AuthProviderPublicInfo[]>([]);
  providersLoading = signal(true);
  activeProviderId = signal<string | null>(null);

  ngOnInit(): void {
    this.sidenavService.hide();
    this.loadProviders();
  }

  ngOnDestroy(): void {
    this.sidenavService.show();
  }

  private async loadProviders(): Promise<void> {
    try {
      const url = `${this.config.appApiUrl()}/auth/providers`;
      const response = await firstValueFrom(
        this.http.get<AuthProviderPublicListResponse>(url)
      );

      const providerList = response?.providers ?? [];
      this.providers.set(providerList);

      // Auto-login if exactly one provider (skip selection screen)
      if (providerList.length === 1) {
        this.storeReturnUrl();
        await this.authService.login(providerList[0].provider_id);
      }
    } catch (error) {
      this.providers.set([]);
    } finally {
      this.providersLoading.set(false);
    }
  }

  async handleProviderLogin(provider: AuthProviderPublicInfo): Promise<void> {
    this.isLoading.set(true);
    this.activeProviderId.set(provider.provider_id);
    this.errorMessage.set(null);

    try {
      this.storeReturnUrl();
      await this.authService.login(provider.provider_id);
    } catch (error) {
      this.isLoading.set(false);
      this.activeProviderId.set(null);
      const errorMsg = error instanceof Error ? error.message : 'An error occurred during login';
      this.errorMessage.set(errorMsg);
    }
  }

  /**
   * Darken or lighten a hex color for hover states.
   */
  adjustBrightness(hex: string, percent: number): string {
    const num = parseInt(hex.replace('#', ''), 16);
    const r = Math.min(255, Math.max(0, (num >> 16) + percent));
    const g = Math.min(255, Math.max(0, ((num >> 8) & 0x00ff) + percent));
    const b = Math.min(255, Math.max(0, (num & 0x0000ff) + percent));
    return `#${((1 << 24) | (r << 16) | (g << 8) | b).toString(16).slice(1)}`;
  }

  private storeReturnUrl(): void {
    const returnUrl = this.route.snapshot.queryParams['returnUrl'];

    let finalDestination: string | undefined;
    if (returnUrl) {
      finalDestination = returnUrl.startsWith('/') ? returnUrl : `/${returnUrl}`;
    } else {
      const referrer = document.referrer;
      if (referrer) {
        try {
          const referrerUrl = new URL(referrer);
          if (referrerUrl.origin === window.location.origin) {
            const referrerPath = referrerUrl.pathname + referrerUrl.search;
            if (referrerPath !== '/auth/login' && referrerPath !== '/auth/callback') {
              finalDestination = referrerPath;
            }
          }
        } catch (e) {
          // Invalid referrer URL, ignore
        }
      }
    }

    if (finalDestination) {
      sessionStorage.setItem('auth_return_url', finalDestination);
    }
  }
}
