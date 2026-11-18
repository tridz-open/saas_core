// Model types are derived from the generated contract (src/generated/schema.d.ts, from saas_core/openapi/v1.yaml).
import type { components } from './generated/schema';
type S = components['schemas'];
export type Interval = S['Interval'];
export type TenantStatus = S['TenantStatus'];
export type JobKind = S['JobKind'];
export type JobState = S['JobState'];
export type SubscriptionStatus = S['SubscriptionStatus'];
export type Gateway = S['Gateway'];
export type Plan = S['Plan'];
export type Template = S['Template'];
export type SignupRequest = S['SignupRequest'];
export type LoginRequest = S['LoginRequest'];
export type Me = S['Me'];
export type CheckoutRequest = S['CheckoutRequest'];
export type RazorpayClientPayload = S['RazorpayClientPayload'];
/** Provider-specific data the browser needs to open an in-page checkout (e.g. Razorpay modal). */
export type ClientPayload = NonNullable<S['CheckoutSession']['client_payload']>;
/**
 * Checkout session. `fallback: 'invoice'` is set by the server when a Razorpay
 * subscription fell back to a one-time order (pay the invoice instead of a recurring mandate).
 */
export type CheckoutSession = Omit<S['CheckoutSession'], 'url'> & { url?: string; fallback?: 'invoice' };
/** Razorpay Checkout handler response, posted back for server-side signature verification. */
export interface RazorpayPaymentResponse {
  razorpay_payment_id: string;
  razorpay_signature: string;
  razorpay_subscription_id?: string;
  razorpay_order_id?: string;
}
export type VerifyPaymentPayload = RazorpayPaymentResponse | Record<string, unknown>;
export type VerifyPaymentResult = S['VerifyPaymentResult'];
export type Tenant = S['Tenant'];
export type PublicConfig = S['PublicConfig'];
export type SlugAvailability = S['SlugAvailability'];
export type AdminTenantCreateRequest = S['AdminTenantCreateRequest'];
export type AdminTenantInviteRequest = S['AdminTenantInviteRequest'];
/** One beta-access allowlist row (admin.beta_access.list). account_exists/verified/has_tenant are null for domain patterns. */
export interface BetaAccessEntry {
  pattern: string;
  kind: 'email' | 'domain';
  note: string | null;
  invited_at: string | null;
  account_exists: boolean | null;
  verified: boolean | null;
  has_tenant: boolean | null;
}
export interface BetaAccessList { signup_mode: string; patterns: BetaAccessEntry[]; }
/** `entries`: emails and/or `@domain` patterns, as an array or a newline/comma/semicolon separated string (max 200). */
export interface BetaAccessAddRequest { entries: string[] | string; send_invite?: boolean; note?: string; }
export interface BetaAccessAddResult {
  added: string[];
  already_listed: string[];
  invites_sent: string[];
  invites_failed: string[];
  skipped_domains: string[];
}
export interface BetaAccessRemoveResult { removed: string; }
export interface BetaAccessResendResult { email: string; sent: boolean; }
export type TenantCreateRequest = S['TenantCreateRequest'];
export type TenantCreated = S['TenantCreated'];
export type SuspendStatus = S['SuspendStatus'];
/** Used for login links, backup download links and billing portal links. */
export type Link = S['LoginLink'];
export type Backup = S['Backup'];
export type Subscription = S['Subscription'];
export type Invoice = S['Invoice'];
export type JobStep = S['JobStep'];
export type Job = S['Job'];
export type SupportTicket = S['SupportTicket'];
export type EventType = S['EventType'];
/**
 * Synthesized by the client's polling fallback for `{ tenantId }` subscriptions only. saas_core's stream no longer
 * emits `tenant.updated`.
 */
export interface TenantUpdatedEvent { seq: number; type: 'tenant.updated'; at: string; tenant?: string; job?: string; data: Tenant }
export type SaasEvent = S['Event'] | TenantUpdatedEvent;
export type CancelPreview = S['CancelPreview'];
export type CancelResult = S['CancelResult'];
export type ChangePreview = S['ChangePreview'];
export type JobUpdatedEvent = S['EventJobUpdated'];
export type SubscriptionUpdatedEvent = S['EventSubscriptionUpdated'];
export type JobStepEvent = S['EventJobStep'];
export type ResyncEvent = S['EventResync'];
export type ApiToken = S['ApiToken'];
export type ApiTokenCreated = S['ApiTokenCreated'];
export type TeamRole = S['TeamRole'];
export type TeamMember = S['TeamMember'];
export type RestoreApproval = S['RestoreApproval'];
export interface ProfileUpdate { full_name?: string; current_password?: string; new_password?: string; }
export interface CreateTokenInput { label: string; tenant?: string; expires_at?: string | null; }
export type ApiErrorBody = S['Error'];
export type BackupPart = 'database' | 'public' | 'private';
export interface CreateTicketInput { tenant?: string; subject: string; body: string; priority?: string; }
export interface Page<T> { items: T[]; next_cursor: string | null; }
export interface ListParams { cursor?: string; limit?: number; }
export const TERMINAL_JOB_STATES: readonly JobState[] = ['succeeded', 'failed', 'needs_attention', 'outcome_unknown'];
export const STABLE_TENANT_STATUSES: readonly TenantStatus[] = ['active', 'failed', 'suspended'];
