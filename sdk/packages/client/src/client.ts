import type * as T from './types';
import { createHttp } from './http';
import { subscribe as subscribeEvents } from './sse';
export type AuthStrategy =
  | { type: 'cookie' }
  | { type: 'apiKey'; key: string; header?: string };
export interface RetryOptions {
  retries?: number;
  baseDelayMs?: number;
  maxDelayMs?: number;
}
export interface ClientOptions {
  baseUrl: string;
  auth?: AuthStrategy;
  fetch?: typeof fetch;
  retry?: RetryOptions;
  pollIntervalMs?: number;
  /**
   * Cookie auth only. Supply a CSRF token (or a function returning one) if you already have it, for example
   * from a server-rendered page. Otherwise the client uses `window.csrf_token` (Frappe-served pages) or
   * fetches it once through `me.get()` before the first mutating request.
   */
  csrfToken?: string | (() => string | undefined | Promise<string | undefined>);
}
export interface Subscription {
  close(): void;
}
/** Public surface. Every method throws SaasError subclasses. Tenant ids are the contract `Tenant.name`. */
export interface SaasClient {
  plans: { list(p?: { currency?: string; interval?: T.Interval }): Promise<T.Plan[]> };
  templates: { list(p?: T.ListParams): Promise<T.Template[]> };
  auth: {
    signup(i: T.SignupRequest): Promise<T.Me>;
    login(i: T.LoginRequest): Promise<T.Me>;
    logout(): Promise<void>;
    requestPasswordReset(email: string): Promise<void>;
    confirmPasswordReset(i: { key: string; new_password: string }): Promise<void>;
    requestEmailVerification(): Promise<void>;
    confirmEmail(key: string): Promise<void>;
  };
  me: { get(): Promise<T.Me>; update(i: T.ProfileUpdate): Promise<T.Me> };
  /** @experimental Disabled by default on saas_core (403 `experimental_disabled`, flag `api_tokens`). */
  apiTokens: {
    list(p?: T.ListParams): Promise<T.ApiToken[]>;
    create(i: T.CreateTokenInput): Promise<T.ApiTokenCreated>;
    revoke(token: string): Promise<void>;
  };
  /** @experimental Disabled by default on saas_core (403 `experimental_disabled`, flag `team`). */
  team: {
    list(tenantId: string, p?: T.ListParams): Promise<T.TeamMember[]>;
    invite(tenantId: string, i: { email: string; role: T.TeamRole }): Promise<T.TeamMember>;
    revokeInvite(tenantId: string, invite: string): Promise<void>;
    changeRole(tenantId: string, member: string, role: T.TeamRole): Promise<T.TeamMember>;
    remove(tenantId: string, member: string): Promise<void>;
    acceptInvite(token: string): Promise<T.TeamMember>;
  };
  /** Public site config (no auth): signup toggle and payment-mock banner. */
  config: { get(): Promise<T.PublicConfig> };
  /** SaaS Admin only (403 otherwise). create may fail 503 capacity_exhausted. */
  admin: {
    tenants: {
      list(p?: T.ListParams & { status?: T.TenantStatus; owner_email?: string }): Promise<T.Tenant[]>;
      listPage(p?: T.ListParams & { status?: T.TenantStatus; owner_email?: string }): Promise<T.Page<T.Tenant>>;
      create(i: T.AdminTenantCreateRequest): Promise<T.TenantCreated>;
      invite(i: T.AdminTenantInviteRequest): Promise<T.TeamMember>;
    };
    /** System Manager only (403 otherwise, including SaaS Admin). Manages the signup allowlist. */
    betaAccess: {
      list(): Promise<T.BetaAccessList>;
      add(i: T.BetaAccessAddRequest): Promise<T.BetaAccessAddResult>;
      /** 404 when the pattern is not listed. */
      remove(pattern: string): Promise<T.BetaAccessRemoveResult>;
      /** Exact emails only; 404 when not listed, 422 for a non-email. */
      resend(email: string): Promise<T.BetaAccessResendResult>;
    };
  };
  tenants: {
    list(p?: T.ListParams & { status?: T.TenantStatus }): Promise<T.Tenant[]>;
    listPage(p?: T.ListParams & { status?: T.TenantStatus }): Promise<T.Page<T.Tenant>>;
    /** Is `<slug>.<site_domain>` free? (reason: invalid | reserved | taken; suggestion: free alternative) */
    checkSlug(slug: string): Promise<T.SlugAvailability>;
    get(id: string): Promise<T.Tenant>;
    create(i: T.TenantCreateRequest): Promise<T.TenantCreated>;
    requestDelete(id: string, i: { confirm_slug: string; reason?: string }): Promise<T.Job>;
    suspendStatus(id: string): Promise<T.SuspendStatus>;
    openLoginLink(id: string): Promise<T.Link>;
    changePlan(id: string, plan: string): Promise<T.Job>;
  };
  backups: {
    list(tenantId: string, p?: T.ListParams): Promise<T.Backup[]>;
    create(tenantId: string): Promise<T.Job>;
    /** @experimental Disabled by default on saas_core (flag `restore`). */
    downloadLink(tenantId: string, backup: string, part: T.BackupPart): Promise<T.Link>;
    /** @experimental Disabled by default on saas_core (flag `restore`). Guarded: returns a pending approval request, never a job. */
    restore(tenantId: string, backup: string): Promise<T.RestoreApproval>;
  };
  checkout: {
    createSession(i: T.CheckoutRequest): Promise<T.CheckoutSession>;
    verifyPayment(gateway: string, payload: T.VerifyPaymentPayload): Promise<T.VerifyPaymentResult>;
  };
  subscription: {
    get(tenantId: string): Promise<T.Subscription>;
  };
  /** Day-based billing: preview then apply a cancel or plan change. Replaces the removed subscription.cancel. */
  billing: {
    previewCancel(tenantId: string): Promise<T.CancelPreview>;
    cancel(tenantId: string, i?: { reason?: string }): Promise<T.CancelResult>;
    previewChange(tenantId: string, plan: string): Promise<T.ChangePreview>;
  };
  invoices: {
    list(p?: T.ListParams & { tenant?: string }): Promise<T.Invoice[]>;
    get(id: string): Promise<T.Invoice>;
    pdf(id: string): Promise<Blob>;
  };
  paymentMethods: { portalLink(i?: { tenant?: string; return_url?: string }): Promise<T.Link> };
  jobs: { get(id: string): Promise<T.Job>; list(p?: T.ListParams & { tenant?: string }): Promise<T.Job[]> };
  /** @experimental Disabled by default on saas_core (403 `experimental_disabled`, flag `support`). */
  support: {
    list(p?: T.ListParams): Promise<T.SupportTicket[]>;
    get(id: string): Promise<T.SupportTicket>;
    create(i: T.CreateTicketInput): Promise<T.SupportTicket>;
    reply(ticket: string, body: string): Promise<T.SupportTicket>;
    close(ticket: string): Promise<T.SupportTicket>;
  };
  /** @experimental SSE is disabled by default on saas_core (flag `sse`); the client then falls back to polling. */
  events: { stream: SaasClient['subscribe'] };
  /** SSE GET events.stream?job=|tenant= with auto-reconnect (Last-Event-ID), falls back to polling jobs.get/tenants.get. */
  subscribe(
    target: { jobId: string } | { tenantId: string },
    onEvent: (e: T.SaasEvent) => void,
    onError?: (e: unknown) => void,
  ): Subscription;
}

export const createClient = (o: ClientOptions): SaasClient => {
  const http = createHttp(o);
  const get = <X>(p: string, query?: Record<string, string | undefined>) => http.request<X>('GET', p, { query });
  const post = <X>(p: string, body?: unknown) => http.request<X>('POST', p, { body: body ?? {} });
  // Only operations whose saas_core handler honours Idempotency-Key are retried automatically.
  const postIdem = <X>(p: string, body?: unknown) => http.request<X>('POST', p, { body: body ?? {}, idempotent: true });
  const q = (r: object | undefined) =>
    Object.fromEntries(Object.entries(r ?? {}).map(([k, v]) => [k, v === undefined ? undefined : String(v)])) as Record<
      string,
      string | undefined
    >;
  const page = <X>(p: string, params?: object) => get<T.Page<X>>(p, q(params));
  const items = async <X>(p: string, params?: object) => (await page<X>(p, params)).items ?? [];
  const remember = async (r: Promise<T.Me>) => {
    const me = await r;
    http.setCsrf(me.csrf_token);
    return me;
  };
  const subscribe: SaasClient['subscribe'] = (target, onEvent, onError) =>
    subscribeEvents(http, target, onEvent, onError, { retry: o.retry, pollIntervalMs: o.pollIntervalMs });
  return {
    plans: { list: (p) => items('plans.list', p) },
    templates: { list: (p) => items('templates.list', p) },
    auth: {
      signup: (i) => remember(postIdem('signup', i)),
      login: (i) => remember(post('login', i)),
      logout: async () => {
        await post('logout');
        http.setCsrf(undefined);
      },
      requestPasswordReset: async (email) => {
        await post('password_reset.request', { email });
      },
      confirmPasswordReset: async (i) => {
        await post('password_reset.confirm', i);
      },
      requestEmailVerification: async () => {
        await post('verify_email.request');
      },
      confirmEmail: async (key) => {
        await post('verify_email.confirm', { key });
      },
    },
    me: { get: () => remember(get('me')), update: (i) => remember(post('me.update', i)) },
    /** @experimental Disabled by default on saas_core (403 `experimental_disabled`, flag `api_tokens`). */
  apiTokens: {
      list: (p) => items('api_tokens.list', p),
      create: (i) => postIdem('api_tokens.create', i),
      revoke: async (token) => {
        await post('api_tokens.revoke', { token });
      },
    },
    /** @experimental Disabled by default on saas_core (403 `experimental_disabled`, flag `team`). */
  team: {
      list: (t, p) => items('tenants.team.list', { ...p, tenant: t }),
      invite: (t, i) => postIdem('tenants.team.invite', { tenant: t, ...i }),
      revokeInvite: async (t, invite) => {
        await post('tenants.team.revoke_invite', { tenant: t, invite });
      },
      changeRole: (t, member, role) => post('tenants.team.change_role', { tenant: t, member, role }),
      remove: async (t, member) => {
        await post('tenants.team.remove', { tenant: t, member });
      },
      acceptInvite: (token) => post('team.accept_invite', { token }),
    },
    tenants: {
      list: (p) => items('tenants.list', p),
      listPage: (p) => page('tenants.list', p),
      checkSlug: (slug) => get('tenants.slug_availability', { slug }),
      get: (id) => get('tenants.get', { tenant: id }),
      create: (i) => postIdem('tenants.create', i),
      requestDelete: (id, i) => postIdem('tenants.request_delete', { tenant: id, ...i }),
      suspendStatus: (id) => get('tenants.suspend_status', { tenant: id }),
      openLoginLink: (id) => post('tenants.open_login_link', { tenant: id }),
      changePlan: (id, plan) => postIdem('tenants.change_plan', { tenant: id, plan }),
    },
    backups: {
      list: (t, p) => items('tenants.backups.list', { ...p, tenant: t }),
      create: (t) => postIdem('tenants.backups.create', { tenant: t }),
      downloadLink: (t, backup, part) => post('tenants.backups.download_link', { tenant: t, backup, part }),
      restore: (t, backup) => postIdem('tenants.backups.restore', { tenant: t, backup }),
    },
    checkout: {
      createSession: (i) => postIdem('checkout.create_session', i),
      verifyPayment: (gateway, payload) => post('checkout.verify_payment', { gateway, payload }),
    },
    subscription: {
      get: (t) => get('subscription.get', { tenant: t }),
    },
    billing: {
      previewCancel: (t) => get('billing.preview_cancel', { tenant: t }),
      cancel: (t, i) => postIdem('billing.cancel', { tenant: t, ...i }),
      previewChange: (t, plan) => get('billing.preview_change', { tenant: t, plan }),
    },
    invoices: {
      list: (p) => items('invoices.list', p),
      get: (id) => get('invoices.get', { invoice: id }),
      pdf: (id) => http.download('invoices.pdf', { invoice: id }),
    },
    paymentMethods: { portalLink: (i) => post('payment_methods.portal_link', i) },
    jobs: { get: (id) => get('jobs.get', { job: id }), list: (p) => items('jobs.list', p) },
    /** @experimental Disabled by default on saas_core (403 `experimental_disabled`, flag `support`). */
  support: {
      list: (p) => items('support.tickets.list', p),
      get: (id) => get('support.tickets.get', { ticket: id }),
      create: (i) => postIdem('support.tickets.create', i),
      reply: (ticket, body) => post('support.tickets.reply', { ticket, body }),
      close: (ticket) => post('support.tickets.close', { ticket }),
    },
    config: { get: () => get('config') },
    admin: {
      tenants: {
        list: (p) => items('admin.tenants.list', p),
        listPage: (p) => page('admin.tenants.list', p),
        create: (i) => post('admin.tenants.create', i),
        invite: (i) => post('admin.tenants.invite', i),
      },
      betaAccess: {
        list: () => get('admin.beta_access.list'),
        add: (i) => post('admin.beta_access.add', i),
        remove: (pattern) => post('admin.beta_access.remove', { pattern }),
        resend: (email) => post('admin.beta_access.resend', { email }),
      },
    },
    /** @experimental SSE is disabled by default on saas_core (flag `sse`); the client then falls back to polling. */
  events: { stream: subscribe },
    subscribe,
  };
};
