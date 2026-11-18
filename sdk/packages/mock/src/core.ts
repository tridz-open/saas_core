import type {
  ApiToken, Backup, Invoice, Job, JobKind, JobState, JobStep, Me, Plan, RestoreApproval, SaasEvent, Subscription, SupportTicket, Template, TeamMember, TeamRole, Tenant,
} from '@frappe-saaskit/client';

export interface MockOptions {
  /** Optional URL prefix (e.g. `https://api.example.com`); matched by msw handlers. Any prefix matches in `handle`. */
  baseUrl?: string;
  /** Delay between provisioning job ticks (ms). Default 800. */
  stepMs?: number;
  /** SSE heartbeat interval (ms). Default 15000. */
  pingMs?: number;
  /** Treat every protected endpoint as unauthenticated, regardless of session. */
  forceUnauthenticated?: boolean;
  /** Start with a logged-in session. Default true. */
  loggedIn?: boolean;
  /** PublicConfig.public_signup; when false signup returns 403 signup_disabled. Default true. */
  publicSignup?: boolean;
  /** Mock signup gate: 'open' (default, honours publicSignup), 'disabled' -> 403 signup_disabled, 'allowlist' -> 403 not_allowlisted unless email in signupAllowlist, 'invite' -> 403 invite_required unless body.invite_token is in signupInvites. */
  signupMode?: 'open' | 'disabled' | 'allowlist' | 'invite';
  signupAllowlist?: string[];
  signupInvites?: string[];
  /** PublicConfig.payment_banner. Default: mock test-mode notice. */
  paymentBanner?: string | null;
  /** Whether the mock user holds the SaaS Admin role (admin.* endpoints 403 otherwise). Default true. */
  admin?: boolean;
  /** When true, tenant creation (incl. admin.tenants.create) returns 503 capacity_exhausted. */
  capacityExhausted?: boolean;
  /** Experimental flags that are switched on (like saas_core's `saas_core_experimental`). Default none: gated routes return 403 experimental_disabled. Use 'all' to enable everything. */
  experimental?: ExperimentalFlag[] | 'all';
}
export type ExperimentalFlag = 'api_tokens' | 'team' | 'support' | 'sse' | 'restore';
const EXPERIMENTAL_ROUTE = (name: string): ExperimentalFlag | undefined =>
  name.startsWith('api_tokens.') ? 'api_tokens'
  : name.startsWith('tenants.team.') || name === 'team.accept_invite' ? 'team'
  : name.startsWith('support.') ? 'support'
  : name === 'events.stream' ? 'sse'
  : name === 'tenants.backups.restore' || name === 'tenants.backups.download_link' ? 'restore'
  : undefined;

export interface MockStore {
  loggedIn: boolean;
  user: { email: string; password: string; full_name: string };
  users: Map<string, { email: string; password: string; full_name: string }>;
  plans: Plan[];
  templates: Template[];
  tenants: Map<string, Tenant>;
  jobs: Map<string, Job>;
  backups: Map<string, Backup[]>;
  invoices: Invoice[];
  subscriptions: Map<string, Subscription>;
  tickets: SupportTicket[];
  events: SaasEvent[];
  tokens: ApiToken[];
  team: Map<string, TeamMember[]>;
  invites: Map<string, string>;
  approvals: string[];
  config: { public_signup: boolean; payment_banner: string | null; site_domain: string | null };
  isAdmin: boolean;
  capacityExhausted: boolean;
}

export interface MockApi {
  options: Required<Pick<MockOptions, 'stepMs' | 'pingMs' | 'forceUnauthenticated'>> & { baseUrl: string };
  store: MockStore;
  /** Returns null when the request is not a saas_core.api.v1 call. */
  handle(req: Request): Promise<Response | null>;
  reset(): void;
}

export const SLUG_RE = /^[a-z][a-z0-9-]{2,30}[a-z0-9]$/;
const PREFIX_RE = /\/api\/method\/saas_core\.api\.v1\.(.+)$/;

class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public field_errors?: Record<string, string>) { super(message); }
}
const fail = (status: number, code: string, message: string, field_errors?: Record<string, string>): never => {
  throw new ApiError(status, code, message, field_errors);
};
const invalid = (field: string, message: string): never => fail(422, 'validation_error', 'Validation failed', { [field]: message });

type Rec = Record<string, unknown>;
interface R { status: number; body?: unknown }
type H = (p: Rec) => R | Response;
interface Route { m: 'GET' | 'POST'; auth: boolean; run: H }

const iso = (d = Date.now()) => new Date(d).toISOString();
const DAY = 86_400_000;

const STEPS: Record<JobKind, string[]> = {
  provision: ['Reserve capacity', 'Create site', 'Install apps', 'Apply template', 'Configure domain'],
  restore: ['Validate backup', 'Restore database', 'Restore files', 'Verify'],
  change_plan: ['Validate plan', 'Apply limits', 'Update subscription'],
  backup: ['Freeze writes', 'Dump database', 'Archive files', 'Upload'],
  terminate: ['Archive data', 'Revoke domain', 'Drop site'],
  suspend: ['Stop workers', 'Suspend site'],
  resume: ['Start workers', 'Resume site'],
};

const PDF = (n: string) => new TextEncoder().encode(
  `%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n` +
  `3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n` +
  `4 0 obj<</Length 44>>stream\nBT /F1 12 Tf 10 50 Td (${n.replace(/[()\\]/g, '')}) Tj ET\nendstream endobj\n` +
  `5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n`);

function seed(loggedIn: boolean, opts: MockOptions = {}): { store: MockStore; counters: { job: number; ticket: number; inv: number; backup: number; token: number; member: number; approval: number } } {
  const now = Date.now();
  const plans: Plan[] = [
    { code: 'free', title: 'Free', description: 'Try it out', prices: [{ interval: 'month', amount: 0, currency: 'USD' }, { interval: 'year', amount: 0, currency: 'USD' }],
      limits: { users: 2, storage_mb: 512, backups: 1 }, trial_days: 0, template: null, features: ['2 users', '512 MB storage', 'Community support'], available: true },
    { code: 'pro', title: 'Pro', description: 'For growing teams', prices: [{ interval: 'month', amount: 2900, currency: 'USD' }, { interval: 'year', amount: 29000, currency: 'USD' }],
      limits: { users: 10, storage_mb: 10240, backups: 7 }, trial_days: 14, template: null, features: ['10 users', '10 GB storage', 'Daily backups', 'Email support'], available: true },
    { code: 'business', title: 'Business', description: 'For organisations', prices: [{ interval: 'month', amount: 9900, currency: 'USD' }, { interval: 'year', amount: 99000, currency: 'USD' }],
      limits: { users: 50, storage_mb: 102400, backups: 30 }, trial_days: 14, template: null, features: ['50 users', '100 GB storage', 'Custom domain', 'Priority support'], available: true },
  ];
  const templates: Template[] = [
    { slug: 'blank', title: 'Blank site', description: 'Empty workspace', min_plan: null, signup_schema: { type: 'object', properties: {} } },
    { slug: 'crm', title: 'CRM', description: 'Leads, deals and contacts', min_plan: 'pro', signup_schema: { type: 'object', properties: { company: { type: 'string' } } } },
    { slug: 'helpdesk', title: 'Helpdesk', description: 'Ticketing out of the box', min_plan: 'free', signup_schema: { type: 'object', properties: {} } },
  ];
  const demo = { email: 'demo@example.com', password: 'correct-horse-battery', full_name: 'Demo User' };
  const mkTenant = (slug: string, plan: string, status: Tenant['status'], extra: Partial<Tenant> = {}): Tenant => ({
    name: slug, slug, status, plan, template: 'blank', region: 'eu-west', url: status === 'active' ? `https://${slug}.mock.test` : null,
    custom_domain: null, created_at: iso(now - 30 * DAY), active_job: null, suspend_reason: null, ...extra });
  const tenants = new Map<string, Tenant>([
    ['acme', mkTenant('acme', 'pro', 'active')],
    ['legacy-co', mkTenant('legacy-co', 'business', 'suspended', { suspend_reason: 'dunning', url: null })],
  ]);
  const steps = (): JobStep[] => STEPS.provision.map((name, i) => ({ seq: i + 1, name, state: 'succeeded' as JobState, started_at: iso(now - 30 * DAY), finished_at: iso(now - 30 * DAY + 1000), message: null }));
  const jobs = new Map<string, Job>([
    ['JOB-0001', { name: 'JOB-0001', tenant: 'acme', kind: 'provision', state: 'succeeded', progress: 1, steps: steps(), attempts: 1, needs_attention: false, error: null, created_at: iso(now - 30 * DAY), finished_at: iso(now - 30 * DAY + 5000) }],
    ['JOB-0002', { name: 'JOB-0002', tenant: 'legacy-co', kind: 'provision', state: 'succeeded', progress: 1, steps: steps(), attempts: 1, needs_attention: false, error: null, created_at: iso(now - 60 * DAY), finished_at: iso(now - 60 * DAY + 5000) }],
  ]);
  const backups = new Map<string, Backup[]>([
    ['acme', [
      { name: 'bk-acme-0002', created_at: iso(now - DAY), size_bytes: 52_428_800, kind: 'scheduled', parts: ['database', 'public', 'private'] },
      { name: 'bk-acme-0001', created_at: iso(now - 2 * DAY), size_bytes: 51_380_224, kind: 'on_demand', parts: ['database', 'public', 'private'] },
    ]],
    ['legacy-co', []],
  ]);
  const inv = (n: number, tenant: string, status: Invoice['status'], amount: number, ago: number): Invoice => ({
    name: `INV-2026-${String(n).padStart(4, '0')}`, tenant, number: `2026-${String(n).padStart(4, '0')}`, status, total: { amount, currency: 'USD' },
    issued_at: iso(now - ago * DAY), paid_at: status === 'paid' ? iso(now - ago * DAY + 3600_000) : null,
    period_start: iso(now - (ago + 30) * DAY), period_end: iso(now - ago * DAY), has_pdf: status !== 'draft', payment_mocked: true });
  const invoices = [inv(3, 'acme', 'paid', 2900, 1), inv(2, 'acme', 'paid', 2900, 31), inv(1, 'acme', 'paid', 2900, 61), inv(4, 'legacy-co', 'open', 9900, 5)];
  const subscriptions = new Map<string, Subscription>([
    ['acme', { tenant: 'acme', plan: 'pro', status: 'active', gateway: 'razorpay', payment_mocked: true, interval: 'month', period_end: iso(now + 29 * DAY), cancel_at_period_end: false, dunning_stage: 0, trial_end: null }],
    ['legacy-co', { tenant: 'legacy-co', plan: 'business', status: 'past_due', gateway: 'razorpay', payment_mocked: true, interval: 'month', period_end: iso(now - 5 * DAY), cancel_at_period_end: false, dunning_stage: 2, trial_end: null }],
  ]);
  const tickets: SupportTicket[] = [
    { name: 'TKT-0001', tenant: 'acme', subject: 'How do I add a custom domain?', status: 'resolved', priority: 'normal', created_at: iso(now - 10 * DAY),
      replies: [{ from_staff: false, body: 'Where do I configure a custom domain?', at: iso(now - 10 * DAY) }, { from_staff: true, body: 'Open Settings > Domains and add your CNAME.', at: iso(now - 9 * DAY) }] },
    { name: 'TKT-0002', tenant: null, subject: 'Invoice question', status: 'open', priority: 'low', created_at: iso(now - 2 * DAY),
      replies: [{ from_staff: false, body: 'Can I get invoices in EUR?', at: iso(now - 2 * DAY) }] },
  ];
  const tokens: ApiToken[] = [
    { name: 'TOK-0001', label: 'CI deploy', tenant: 'acme', prefix: 'a1b2c3d4', created_at: iso(now - 20 * DAY), last_used_at: iso(now - DAY), expires_at: null },
    { name: 'TOK-0002', label: 'Reporting script', tenant: null, prefix: 'e5f6a7b8', created_at: iso(now - 5 * DAY), last_used_at: null, expires_at: iso(now + 90 * DAY) },
  ];
  const ownerOf = (n: number): TeamMember => ({ name: `TM-${String(n).padStart(4, '0')}`, email: demo.email, full_name: demo.full_name, role: 'owner', status: 'active', invited_at: null });
  const team = new Map<string, TeamMember[]>([['acme', [ownerOf(1)]], ['legacy-co', [ownerOf(2)]]]);
  return { store: { loggedIn, user: demo, users: new Map([[demo.email, demo]]), plans, templates, tenants, jobs, backups, invoices, subscriptions, tickets, events: [],
      tokens, team, invites: new Map(), approvals: [],
      config: { public_signup: opts.publicSignup ?? true, payment_banner: opts.paymentBanner === undefined ? 'Test mode: payments are mocked, no real charges.' : opts.paymentBanner, site_domain: 'mock.example.com' },
      isAdmin: opts.admin ?? true, capacityExhausted: opts.capacityExhausted ?? false },
    counters: { job: 2, ticket: 2, inv: 4, backup: 2, token: 2, member: 2, approval: 0 } };
}

export function createMockApi(opts: MockOptions = {}): MockApi {
  const options = { baseUrl: opts.baseUrl ?? '', stepMs: opts.stepMs ?? 800, pingMs: opts.pingMs ?? 15000, forceUnauthenticated: opts.forceUnauthenticated ?? false };
  let { store, counters } = seed(opts.loggedIn ?? true, opts);
  let seq = 0;
  const timers = new Set<ReturnType<typeof setTimeout>>();
  const closers = new Set<() => void>();
  const subs = new Set<(e: SaasEvent) => void>();
  const idem = new Map<string, { body: string; status: number; payload?: string }>();
  const phases = new Map<string, number>();
  const sessions = new Map<string, string>();
  const verified = new Map<string, string | undefined>();
  const api = { options, get store() { return store; } } as unknown as MockApi;

  const later = (fn: () => void, ms: number) => {
    const t: ReturnType<typeof setTimeout> = setTimeout(() => { timers.delete(t); fn(); }, ms);
    timers.add(t);
  };

  function emit(type: Exclude<SaasEvent['type'], 'tenant.updated'>, data: unknown, tenant?: string, job?: string): void {
    const e = { seq: ++seq, type, at: iso(), tenant, job, data: JSON.parse(JSON.stringify(data)) } as SaasEvent;
    store.events.push(e);
    if (store.events.length > 500) store.events.shift();
    subs.forEach((s) => s(e));
  }
  const emitJob = (j: Job) => emit('job.updated', j, j.tenant, j.name);

  function startJob(tenant: Tenant, kind: JobKind, hooks: { create?: () => void; run?: () => void; done: () => void }): Job {
    const n = ++counters.job;
    const job: Job = {
      name: `JOB-${String(n).padStart(4, '0')}`, tenant: tenant.name, kind, state: 'pending', progress: 0,
      steps: STEPS[kind].map((name, i) => ({ seq: i + 1, name, state: 'pending' as JobState, started_at: null, finished_at: null, message: null })),
      attempts: 1, needs_attention: false, error: null, created_at: iso(), finished_at: null };
    store.jobs.set(job.name, job);
    tenant.active_job = job.name;
    hooks.create?.();
    phases.set(job.name, 0);
    emitJob(job);
    const total = job.steps!.length;
    const step = (s: JobStep, state: JobState) => {
      s.state = state;
      if (state === 'running') s.started_at = iso(); else s.finished_at = iso();
      emit('job.step', s, job.tenant, job.name);
    };
    const tick = () => {
      const ph = (phases.get(job.name) ?? 0) + 1;
      phases.set(job.name, ph);
      const steps = job.steps!;
      if (ph === 1) job.state = 'dispatching';
      else if (ph === 2) job.state = 'submitted';
      else if (ph <= 2 + total) {
        const k = ph - 3; // index of step starting now
        job.state = 'running';
        if (k === 0) hooks.run?.(); else step(steps[k - 1]!, 'succeeded');
        step(steps[k]!, 'running');
        job.progress = Math.round((k / total) * 1000) / 1000;
       
      } else {
        step(steps[total - 1]!, 'succeeded');
        job.state = 'succeeded'; job.progress = 1; job.finished_at = iso();
        if (store.tenants.get(tenant.name)) tenant.active_job = null;
        hooks.done();
      }
      emitJob(job);
      if (job.state !== 'succeeded') later(tick, options.stepMs);
    };
    later(tick, options.stepMs);
    return JSON.parse(JSON.stringify(job));
  }

  // ---------- helpers ----------
  const str = (p: Rec, k: string, required = true): string => {
    const v = p[k];
    if (v === undefined || v === null || v === '') { if (required) invalid(k, 'This field is required'); return ''; }
    if (typeof v !== 'string') invalid(k, 'Must be a string');
    return v as string;
  };
  const paginate = <T>(all: T[], p: Rec): R => {
    const limit = p.limit === undefined || p.limit === '' ? 20 : Number(p.limit);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) invalid('limit', 'Must be an integer between 1 and 100');
    const start = p.cursor ? Number(p.cursor) : 0;
    if (!Number.isInteger(start) || start < 0) invalid('cursor', 'Invalid cursor');
    const items = all.slice(start, start + limit);
    return { status: 200, body: { message: { items, next_cursor: start + limit < all.length ? String(start + limit) : null } } };
  };
  const ok = (message: unknown, status = 200): R => ({ status, body: { message } });
  const tenantOf = (p: Rec, k = 'tenant'): Tenant => {
    const name = str(p, k);
    return store.tenants.get(name) ?? fail(404, 'not_found', 'Tenant not found');
  };
  const checkSlug = (slug: string, field = 'slug') => {
    if (!SLUG_RE.test(slug)) invalid(field, 'Must be 4-32 chars: lowercase letters, digits, hyphens; start with a letter');
    if (store.tenants.has(slug)) fail(409, 'conflict', 'Slug already taken', { [field]: 'Slug already taken' });
  };
  const requireAdmin = () => { if (!store.isAdmin) fail(403, 'forbidden', 'SaaS Admin role required'); };
  const planOf = (code: string, field = 'plan'): Plan => store.plans.find((x) => x.code === code) ?? invalid(field, 'Unknown plan');
  const idle = (t: Tenant) => { if (t.active_job) fail(409, 'conflict', 'Another operation is already in progress for this tenant'); };
  const me = (): Me => ({ user: store.user.email, email: store.user.email, full_name: store.user.full_name, roles: ['SaaS Customer'], tenant_count: store.tenants.size, csrf_token: 'mock-csrf-token' });
  const link = (url: string, mins = 10) => ok({ url, expires_at: iso(Date.now() + mins * 60_000) });
  const newTenant = (slug: string, plan: string, template: string | null | undefined, region?: string | null): Tenant => ({
    name: slug, slug, status: 'pending', plan, template: template ?? null, region: region ?? 'eu-west', url: null, custom_domain: null, created_at: iso(), active_job: null, suspend_reason: null });
  function subscribeTenant(t: Tenant, plan: Plan, interval: 'month' | 'year' = 'month') {
    const trial = plan.trial_days ?? 0;
    store.subscriptions.set(t.name, { tenant: t.name, plan: plan.code, status: trial ? 'trialing' : 'active', gateway: 'razorpay', payment_mocked: true, interval,
      period_end: iso(Date.now() + 30 * DAY), cancel_at_period_end: false, dunning_stage: 0, trial_end: trial ? iso(Date.now() + trial * DAY) : null });
    store.backups.set(t.name, store.backups.get(t.name) ?? []);
  }
  function provision(t: Tenant, kind: 'provision'): Job {
    t.status = 'pending';
    store.tenants.set(t.name, t);
    return startJob(t, kind, {
      run: () => { t.status = 'provisioning'; },
      done: () => { t.status = 'active'; t.url = `https://${t.slug}.mock.test`; },
    });
  }
  const templateOf = (slug: string | undefined, planCode: string) => {
    if (!slug) return undefined;
    const tpl = store.templates.find((x) => x.slug === slug) ?? invalid('template', 'Unknown template');
    const order = store.plans.map((x) => x.code);
    if (tpl.min_plan && order.indexOf(planCode) < order.indexOf(tpl.min_plan)) invalid('template', `Requires plan ${tpl.min_plan} or higher`);
    return tpl.slug;
  };

  function cancelPreview(t: Tenant) {
    const s = store.subscriptions.get(t.name) ?? fail(404, 'not_found', 'Subscription not found');
    const blocked = s!.cancel_at_period_end || s!.status === 'canceled' ? 'already_canceling' as const : null;
    const paid = s!.status === 'trialing' ? 0 : 99900;
    const refund = blocked ? 0 : Math.round(paid * 0.5);
    return { tenant: t.name, subscription: t.name, plan: s!.plan, plan_label: s!.plan, currency: 'INR', can_cancel: !blocked, blocked_reason: blocked,
      cycle_start: null, cycle_end: null, cycle_days: 30, days_used: 15, days_unused: 15, paid_minor: paid, charge_minor: paid - refund, refund_minor: refund,
      stops_at: iso(Date.now() + DAY), timezone: 'Asia/Kolkata', refund_to: 'original payment method', renewal_date: null };
  }

  // ---------- routes ----------
  const routes: Record<string, Route> = {
    'plans.list': { m: 'GET', auth: false, run: (p) => {
      let list = store.plans;
      if (p.interval) { if (p.interval !== 'month' && p.interval !== 'year') invalid('interval', 'Must be month or year'); list = list.map((x) => ({ ...x, prices: x.prices.filter((pr) => pr.interval === p.interval) })); }
      if (p.currency) list = list.map((x) => ({ ...x, prices: x.prices.filter((pr) => pr.currency.toLowerCase() === String(p.currency).toLowerCase()) }));
      return ok({ items: list, next_cursor: null });
    } },
    'templates.list': { m: 'GET', auth: false, run: (p) => paginate(store.templates, p) },
    config: { m: 'GET', auth: false, run: () => ok({ ...store.config }) },
    signup: { m: 'POST', auth: false, run: (p) => {
      const mode = opts.signupMode ?? 'open';
      if (mode === 'disabled' || !store.config.public_signup) fail(403, 'signup_disabled', 'Public signup is disabled');
      const email = str(p, 'email').toLowerCase(), password = str(p, 'password');
      if (mode === 'allowlist' && !(opts.signupAllowlist ?? []).map((e) => e.toLowerCase()).includes(email)) fail(403, 'not_allowlisted', 'This email is not on the signup allowlist');
      if (mode === 'invite' && !(typeof p.invite_token === 'string' && (opts.signupInvites ?? []).includes(p.invite_token))) fail(403, 'invite_required', 'A valid invite is required to sign up');
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) invalid('email', 'Invalid email');
      if (password.length < 10) invalid('password', 'Must be at least 10 characters');
      if (p.plan !== undefined) planOf(str(p, 'plan'));
      if (p.template !== undefined) templateOf(str(p, 'template'), str(p, 'plan', false) || 'free');
      if (store.users.has(email)) fail(409, 'conflict', 'An account with this email already exists', { email: 'Already registered' });
      const u = { email, password, full_name: typeof p.full_name === 'string' && p.full_name ? p.full_name : email.split('@')[0]! };
      store.users.set(email, u);
      store.user = u; store.loggedIn = true;
      return ok(me(), 201);
    } },
    login: { m: 'POST', auth: false, run: (p) => {
      const email = str(p, 'email').toLowerCase(), password = str(p, 'password');
      const u = store.users.get(email);
      if (!u || u.password !== password) fail(401, 'unauthorized', 'Invalid email or password');
      store.user = u!; store.loggedIn = true;
      return ok(me());
    } },
    logout: { m: 'POST', auth: false, run: () => { store.loggedIn = false; return { status: 204 }; } },
    'password_reset.request': { m: 'POST', auth: false, run: (p) => { str(p, 'email'); return { status: 202, body: { message: { status: 'accepted' } } }; } },
    'verify_email.request': { m: 'POST', auth: true, run: () => ({ status: 202 }) },
    'verify_email.confirm': { m: 'POST', auth: true, run: (p) => { str(p, 'key'); return { status: 204 }; } },
    'password_reset.confirm': { m: 'POST', auth: false, run: (p) => {
      str(p, 'key');
      const pw = str(p, 'new_password');
      if (pw.length < 10) invalid('new_password', 'Must be at least 10 characters');
      if (p.key === 'invalid') fail(404, 'not_found', 'Reset key is invalid or expired');
      store.user.password = pw;
      return { status: 204 };
    } },
    me: { m: 'GET', auth: true, run: () => ok(me()) },
    'me.update': { m: 'POST', auth: true, run: (p) => {
      const u = store.user;
      if (p.full_name !== undefined) u.full_name = str(p, 'full_name');
      if (p.new_password !== undefined || p.current_password !== undefined) {
        const np = str(p, 'new_password'), cur = str(p, 'current_password');
        if (np.length < 10) invalid('new_password', 'Must be at least 10 characters');
        if (cur !== u.password) invalid('current_password', 'Current password is incorrect');
        u.password = np;
      }
      return ok(me());
    } },
    'api_tokens.list': { m: 'GET', auth: true, run: (p) => paginate([...store.tokens].reverse(), p) },
    'api_tokens.create': { m: 'POST', auth: true, run: (p) => {
      const label = str(p, 'label');
      if (label.length > 100) invalid('label', 'Must be at most 100 characters');
      if (p.tenant) tenantOf(p);
      const rnd = () => Math.random().toString(36).slice(2, 10).padEnd(8, '0');
      const api_key = rnd();
      const token: ApiToken = { name: `TOK-${String(++counters.token).padStart(4, '0')}`, label, tenant: (p.tenant as string | undefined) ?? null, prefix: api_key,
        created_at: iso(), last_used_at: null, expires_at: typeof p.expires_at === 'string' ? p.expires_at : null };
      store.tokens.push(token);
      return ok({ token, secret: rnd() + rnd(), api_key }, 201);
    } },
    'api_tokens.revoke': { m: 'POST', auth: true, run: (p) => {
      const i = store.tokens.findIndex((x) => x.name === str(p, 'token'));
      if (i < 0) fail(404, 'not_found', 'Token not found');
      store.tokens.splice(i, 1);
      return { status: 204 };
    } },
    'tenants.team.list': { m: 'GET', auth: true, run: (p) => paginate(store.team.get(tenantOf(p).name) ?? [], p) },
    'tenants.team.invite': { m: 'POST', auth: true, run: (p) => {
      const t = tenantOf(p);
      const email = str(p, 'email').toLowerCase(), role = str(p, 'role');
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) invalid('email', 'Invalid email');
      if (!['owner', 'admin', 'member', 'viewer'].includes(role)) invalid('role', 'Unknown role');
      if (role === 'owner') invalid('role', 'Cannot invite an owner');
      const list = store.team.get(t.name) ?? [];
      if (list.some((x) => x.email === email)) fail(409, 'conflict', 'Already a member or invited', { email: 'Already a member or invited' });
      const m: TeamMember = { name: `TM-${String(++counters.member).padStart(4, '0')}`, email, full_name: null, role: role as TeamRole, status: 'invited', invited_at: iso() };
      list.push(m);
      store.team.set(t.name, list);
      store.invites.set(`invite-${m.name}`, t.name);
      return ok(m, 201);
    } },
    'tenants.team.revoke_invite': { m: 'POST', auth: true, run: (p) => {
      const t = tenantOf(p);
      const list = store.team.get(t.name) ?? [];
      const i = list.findIndex((x) => x.name === str(p, 'invite') && x.status === 'invited');
      if (i < 0) fail(404, 'not_found', 'Invite not found');
      store.invites.delete(`invite-${list[i]!.name}`);
      list.splice(i, 1);
      return { status: 204 };
    } },
    'tenants.team.change_role': { m: 'POST', auth: true, run: (p) => {
      const t = tenantOf(p);
      const role = str(p, 'role');
      if (!['owner', 'admin', 'member', 'viewer'].includes(role)) invalid('role', 'Unknown role');
      const m = (store.team.get(t.name) ?? []).find((x) => x.name === str(p, 'member')) ?? fail(404, 'not_found', 'Member not found');
      if (m!.role === 'owner') fail(403, 'forbidden', 'The owner role cannot be changed');
      if (role === 'owner') fail(403, 'forbidden', 'Ownership cannot be assigned');
      m!.role = role as TeamRole;
      return ok(m);
    } },
    'tenants.team.remove': { m: 'POST', auth: true, run: (p) => {
      const t = tenantOf(p);
      const list = store.team.get(t.name) ?? [];
      const i = list.findIndex((x) => x.name === str(p, 'member'));
      if (i < 0) fail(404, 'not_found', 'Member not found');
      if (list[i]!.role === 'owner') fail(403, 'forbidden', 'The owner cannot be removed');
      store.invites.delete(`invite-${list[i]!.name}`);
      list.splice(i, 1);
      return { status: 204 };
    } },
    'team.accept_invite': { m: 'POST', auth: true, run: (p) => {
      const token = str(p, 'token');
      const tn = store.invites.get(token) ?? fail(404, 'not_found', 'Invite is invalid or expired');
      const m = (store.team.get(tn!) ?? []).find((x) => `invite-${x.name}` === token) ?? fail(404, 'not_found', 'Invite is invalid or expired');
      m!.status = 'active'; m!.full_name = m!.full_name ?? m!.email.split('@')[0]!;
      store.invites.delete(token);
      return ok(m);
    } },
    'checkout.create_session': { m: 'POST', auth: true, run: (p) => {
      const plan = planOf(str(p, 'plan'));
      const interval = str(p, 'interval');
      if (interval !== 'month' && interval !== 'year') invalid('interval', 'Must be month or year');
      for (const k of ['success_url', 'cancel_url']) { try { new URL(str(p, k)); } catch { invalid(k, 'Must be a valid URL'); } }
      const gateway = (p.gateway as string | undefined) ?? 'razorpay';
      if (gateway !== 'razorpay') invalid('gateway', 'Unsupported gateway (razorpay only)');
      let tenant: Tenant;
      if (p.tenant) { tenant = tenantOf(p); idle(tenant); }
      else {
        const slug = str(p, 'slug');
        checkSlug(slug);
        const template = templateOf(str(p, 'template', false) || undefined, plan.code);
        tenant = newTenant(slug, plan.code, template);
        subscribeTenant(tenant, plan, interval as 'month' | 'year');
        provision(tenant, 'provision');
      }
      const id = `cs_mock_${Math.random().toString(36).slice(2, 10)}`;
      sessions.set(id, tenant.name);
      return ok({ gateway, session_id: id, url: `https://checkout.mock.test/pay/${id}`, client_payload: { provider: 'manual' }, payment_mocked: true, tenant: tenant.name, public_key: `pk_test_mock_${gateway}`, expires_at: iso(Date.now() + 30 * 60_000) }, 201);
    } },
    'checkout.verify_payment': { m: 'POST', auth: true, run: (p) => {
      const gateway = str(p, 'gateway');
      const pl = (p.payload && typeof p.payload === 'object' ? p.payload : {}) as Record<string, unknown>;
      const sid = typeof pl.session_id === 'string' ? pl.session_id : undefined;
      const key = `${gateway}:${sid ?? JSON.stringify(pl)}`;
      const tenant = (sid && sessions.get(sid)) || (typeof pl.tenant === 'string' ? pl.tenant : undefined);
      if (verified.has(key)) return ok({ status: 'duplicate', tenant: verified.get(key) });
      verified.set(key, tenant);
      return ok({ status: 'ok', tenant });
    } },
    'tenants.list': { m: 'GET', auth: true, run: (p) => {
      if (p.status !== undefined && !['pending', 'provisioning', 'active', 'suspended', 'terminating', 'failed'].includes(String(p.status))) invalid('status', 'Unknown status');
      return paginate([...store.tenants.values()].filter((t) => !p.status || t.status === p.status), p);
    } },
    'tenants.slug_availability': { m: 'GET', auth: true, run: (p) => {
      const slug = String(p.slug ?? '').trim().toLowerCase();
      const reserved = ['www', 'api', 'app', 'admin', 'mail', 'cp', 'console', 'dashboard', 'support', 'help', 'status', 'blog', 'docs', 'billing', 'login', 'signup', 'auth', 'static', 'assets', 'cdn', 'ftp', 'smtp', 'imap', 'root', 'test', 'demo', 'staging', 'dev'];
      const why = (s: string) => (!SLUG_RE.test(s) ? 'invalid' : reserved.includes(s) ? 'reserved' : store.tenants.has(s) ? 'taken' : null);
      const reason = why(slug);
      const msgs = { invalid: 'Use 4\u201332 lowercase letters, numbers or dashes, starting with a letter.', reserved: 'That address is reserved.', taken: 'That address is taken.' };
      let suggestion: string | null = null;
      if (reason) {
        const base = reason === 'invalid' ? slug.replace(/[^a-z0-9]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '').replace(/^(\d)/, 'x$1').slice(0, 32) : slug;
        for (const cand of [base, ...[2, 3, 4, 5, 6, 7, 8, 9].map((n) => `${base.slice(0, 30)}-${n}`)]) { if (why(cand) === null) { suggestion = cand; break; } }
      }
      return ok({ slug, available: !reason, reason, message: reason ? msgs[reason] : 'Available', suggestion });
    } },
    'tenants.get': { m: 'GET', auth: true, run: (p) => ok(tenantOf(p)) },
    'admin.tenants.list': { m: 'GET', auth: true, run: (p) => {
      requireAdmin();
      if (p.status !== undefined && !['pending', 'provisioning', 'active', 'suspended', 'terminating', 'failed'].includes(String(p.status))) invalid('status', 'Unknown status');
      const owner = p.owner_email ? String(p.owner_email).toLowerCase() : '';
      return paginate([...store.tenants.values()].filter((t) => (!p.status || t.status === p.status) && (!owner || (store.team.get(t.name) ?? []).some((m) => m.role === 'owner' && m.email === owner))), p);
    } },
    'admin.tenants.create': { m: 'POST', auth: true, run: (p) => {
      requireAdmin();
      const email = str(p, 'owner_email').toLowerCase();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) invalid('owner_email', 'Invalid email');
      const plan = planOf(str(p, 'plan'));
      const name = str(p, 'name', false);
      const slug = str(p, 'subdomain', false) || (name || email.split('@')[0]!).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').replace(/^[^a-z]+/, '');
      checkSlug(slug, 'subdomain');
      const template = templateOf(str(p, 'template', false) || undefined, plan.code);
      if (store.capacityExhausted) fail(503, 'capacity_exhausted', 'No server has capacity right now');
      const t = newTenant(slug, plan.code, template);
      subscribeTenant(t, plan);
      store.team.set(t.name, [{ name: `TM-${String(++counters.member).padStart(4, '0')}`, email, full_name: null, role: 'owner', status: 'invited', invited_at: iso() }]);
      const job = provision(t, 'provision');
      return ok({ tenant: JSON.parse(JSON.stringify(t)), job, checkout_required: false }, 202);
    } },
    'admin.tenants.invite': { m: 'POST', auth: true, run: (p) => {
      requireAdmin();
      const t = tenantOf(p);
      const email = str(p, 'owner_email').toLowerCase();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) invalid('owner_email', 'Invalid email');
      const list = store.team.get(t.name) ?? [];
      if (list.some((x) => x.email === email)) fail(409, 'conflict', 'Already a member or invited', { owner_email: 'Already a member or invited' });
      const m: TeamMember = { name: `TM-${String(++counters.member).padStart(4, '0')}`, email, full_name: null, role: 'owner', status: 'invited', invited_at: iso() };
      list.push(m);
      store.team.set(t.name, list);
      store.invites.set(`invite-${m.name}`, t.name);
      return ok(m, 201);
    } },
    'tenants.create': { m: 'POST', auth: true, run: (p) => {
      if (store.capacityExhausted) fail(503, 'capacity_exhausted', 'No server has capacity right now');
      const slug = str(p, 'slug');
      const plan = planOf(str(p, 'plan'));
      checkSlug(slug);
      const template = templateOf(str(p, 'template', false) || undefined, plan.code);
      const t = newTenant(slug, plan.code, template, str(p, 'region', false) || null);
      subscribeTenant(t, plan);
      const job = provision(t, 'provision');
      return ok({ tenant: JSON.parse(JSON.stringify(t)), job, checkout_required: false }, 202);
    } },
    'tenants.request_delete': { m: 'POST', auth: true, run: (p) => {
      const t = tenantOf(p);
      if (str(p, 'confirm_slug') !== t.slug) invalid('confirm_slug', 'Does not match the tenant slug');
      idle(t);
      if (t.status === 'terminating') fail(409, 'conflict', 'Tenant is already being deleted');
      const job = startJob(t, 'terminate', {
        create: () => { t.status = 'terminating'; },
        done: () => { store.tenants.delete(t.name); store.subscriptions.delete(t.name); store.backups.delete(t.name); },
      });
      return ok(job, 202);
    } },
    'tenants.suspend_status': { m: 'GET', auth: true, run: (p) => {
      const t = tenantOf(p);
      const s = store.subscriptions.get(t.name);
      const suspended = t.status === 'suspended';
      return ok({ suspended, reason: suspended ? ((t.suspend_reason as 'dunning' | 'manual' | 'abuse' | 'canceled' | null) ?? 'manual') : null,
        dunning_stage: s?.dunning_stage ?? 0, since: suspended ? iso(Date.now() - 5 * DAY) : null, resume_by_paying: suspended && t.suspend_reason === 'dunning' });
    } },
    'tenants.open_login_link': { m: 'POST', auth: true, run: (p) => {
      const t = tenantOf(p);
      if (t.status !== 'active') fail(409, 'conflict', 'Tenant is not active');
      return link(`https://${t.slug}.mock.test/app?sid=mock-${Math.random().toString(36).slice(2, 10)}`, 1);
    } },
    'tenants.backups.list': { m: 'GET', auth: true, run: (p) => paginate(store.backups.get(tenantOf(p).name) ?? [], p) },
    'tenants.backups.create': { m: 'POST', auth: true, run: (p) => {
      const t = tenantOf(p);
      if (t.status !== 'active') fail(409, 'conflict', 'Tenant is not active');
      idle(t);
      const job = startJob(t, 'backup', { done: () => {
        const n = ++counters.backup;
        const list = store.backups.get(t.name) ?? [];
        list.unshift({ name: `bk-${t.slug}-${String(n).padStart(4, '0')}`, created_at: iso(), size_bytes: 10_485_760 + n * 1024, kind: 'on_demand', parts: ['database', 'public', 'private'] });
        store.backups.set(t.name, list);
       
      } });
      return ok(job, 202);
    } },
    'tenants.backups.download_link': { m: 'POST', auth: true, run: (p) => {
      const t = tenantOf(p);
      const name = str(p, 'backup'), part = str(p, 'part');
      if (!['database', 'public', 'private'].includes(part)) invalid('part', 'Must be database, public or private');
      const b = (store.backups.get(t.name) ?? []).find((x) => x.name === name) ?? fail(404, 'not_found', 'Backup not found');
      return link(`https://files.mock.test/backups/${t.slug}/${b!.name}/${part}.sql.gz?sig=mock`, 10);
    } },
    'tenants.backups.restore': { m: 'POST', auth: true, run: (p) => {
      const t = tenantOf(p);
      const name = str(p, 'backup');
      (store.backups.get(t.name) ?? []).find((x) => x.name === name) ?? fail(404, 'not_found', 'Backup not found');
      const approval: RestoreApproval = { approval_request: `APR-${String(++counters.approval).padStart(4, '0')}`, status: 'pending_approval' };
      store.approvals.push(approval.approval_request);
      return ok(approval, 202);
    } },
    'tenants.change_plan': { m: 'POST', auth: true, run: (p) => {
      const t = tenantOf(p);
      const plan = planOf(str(p, 'plan'));
      if (t.status !== 'active') fail(409, 'conflict', 'Tenant is not active');
      if (plan.code === t.plan) fail(409, 'conflict', 'Tenant is already on this plan');
      idle(t);
      const job = startJob(t, 'change_plan', { done: () => {
        t.plan = plan.code;
        const s = store.subscriptions.get(t.name);
        if (s) { s.plan = plan.code; emit('subscription.updated', s, t.name); }
       
      } });
      return ok(job, 202);
    } },
    'subscription.get': { m: 'GET', auth: true, run: (p) => ok(store.subscriptions.get(tenantOf(p).name) ?? fail(404, 'not_found', 'Subscription not found')) },
    'billing.preview_cancel': { m: 'GET', auth: true, run: (p) => ok(cancelPreview(tenantOf(p))) },
    'billing.cancel': { m: 'POST', auth: true, run: (p) => {
      const t = tenantOf(p);
      const s = store.subscriptions.get(t.name) ?? fail(404, 'not_found', 'Subscription not found');
      const pv = cancelPreview(t);
      if (!pv.can_cancel) fail(409, pv.blocked_reason ?? 'already_canceling', 'Cannot cancel right now');
      s!.cancel_at_period_end = true;
      emit('subscription.updated', s, t.name);
      return ok({ ...pv, status: 'canceling', refund: pv.refund_minor > 0 ? { id: `rfnd_mock_${t.slug}`, status: 'pending', amount_minor: pv.refund_minor, currency: pv.currency ?? 'INR' } : null });
    } },
    'billing.preview_change': { m: 'GET', auth: true, run: (p) => {
      const t = tenantOf(p);
      const to = planOf(str(p, 'plan'));
      const from = planOf(t.plan);
      const price = (x: Plan) => x.prices?.[0]?.amount ?? 0;
      const direction = to.code === from.code ? 'same' : price(to) > price(from) ? 'upgrade' : 'downgrade';
      const diff = Math.max(0, price(to) - price(from));
      return ok({ tenant: t.name, from_plan: from.code, to_plan: to.code, direction, effective_at: iso(Date.now() + DAY), days_remaining: 15, cycle_days: 30,
        currency: 'INR', amount_due_minor: direction === 'upgrade' ? Math.round(diff / 2) : 0, credit_minor: 0, new_cycle_price_minor: price(to),
        renewal_date: null, payment_required: direction === 'upgrade', timezone: 'Asia/Kolkata' });
    } },
    'invoices.list': { m: 'GET', auth: true, run: (p) => {
      if (p.tenant) tenantOf(p);
      return paginate(store.invoices.filter((i) => !p.tenant || i.tenant === p.tenant), p);
    } },
    'invoices.get': { m: 'GET', auth: true, run: (p) => ok(store.invoices.find((i) => i.name === str(p, 'invoice')) ?? fail(404, 'not_found', 'Invoice not found')) },
    'invoices.pdf': { m: 'GET', auth: true, run: (p) => {
      const i = store.invoices.find((x) => x.name === str(p, 'invoice')) ?? fail(404, 'not_found', 'Invoice not found');
      if (!i!.has_pdf) fail(404, 'not_found', 'Invoice has no PDF');
      return new Response(PDF(i!.name) as BodyInit, { status: 200, headers: { 'content-type': 'application/pdf', 'content-disposition': `attachment; filename="${i!.name}.pdf"` } });
    } },
    'payment_methods.portal_link': { m: 'POST', auth: true, run: (p) => { if (p.tenant) tenantOf(p); return link('https://billing.mock.test/portal/payment-methods', 30); } },
    'jobs.get': { m: 'GET', auth: true, run: (p) => ok(store.jobs.get(str(p, 'job')) ?? fail(404, 'not_found', 'Job not found')) },
    'jobs.list': { m: 'GET', auth: true, run: (p) => {
      if (p.tenant) tenantOf(p);
      return paginate([...store.jobs.values()].reverse().filter((j) => !p.tenant || j.tenant === p.tenant), p);
    } },
    'support.tickets.list': { m: 'GET', auth: true, run: (p) => paginate([...store.tickets].reverse(), p) },
    'support.tickets.get': { m: 'GET', auth: true, run: (p) => ok(store.tickets.find((t) => t.name === str(p, 'ticket')) ?? fail(404, 'not_found', 'Ticket not found')) },
    'support.tickets.create': { m: 'POST', auth: true, run: (p) => {
      const subject = str(p, 'subject'), body = str(p, 'body');
      if (subject.length > 200) invalid('subject', 'Must be at most 200 characters');
      if (p.tenant) tenantOf(p);
      const t: SupportTicket = { name: `TKT-${String(++counters.ticket).padStart(4, '0')}`, tenant: (p.tenant as string) ?? null, subject, status: 'open',
        priority: typeof p.priority === 'string' ? p.priority : 'normal', created_at: iso(), replies: [{ from_staff: false, body, at: iso() }] };
      store.tickets.push(t);
      return ok(t, 201);
    } },
    'support.tickets.reply': { m: 'POST', auth: true, run: (p) => {
      const t = store.tickets.find((x) => x.name === str(p, 'ticket')) ?? fail(404, 'not_found', 'Ticket not found');
      const body = str(p, 'body');
      if (t!.status === 'closed') fail(409, 'conflict', 'Ticket is closed');
      t!.replies = [...(t!.replies ?? []), { from_staff: false, body, at: iso() }];
      t!.status = 'open';
      return ok(t);
    } },
    'support.tickets.close': { m: 'POST', auth: true, run: (p) => {
      const t = store.tickets.find((x) => x.name === str(p, 'ticket')) ?? fail(404, 'not_found', 'Ticket not found');
      t!.status = 'closed';
      return ok(t);
    } },
    'events.stream': { m: 'GET', auth: true, run: (p) => {
      const job = p.job ? String(p.job) : undefined, tenant = p.tenant ? String(p.tenant) : undefined;
      const since = p.since !== undefined && p.since !== '' ? Number(p.since) : p['Last-Event-ID'] ? Number(p['Last-Event-ID']) : undefined;
      if (since !== undefined && !Number.isFinite(since)) invalid('since', 'Must be an integer');
      const match = (e: SaasEvent) => (!job || e.job === job) && (!tenant || e.tenant === tenant);
      const enc = new TextEncoder();
      let cleanup = () => {};
      const stream = new ReadableStream<Uint8Array>({
        start(ctl) {
          const send = (s: string) => { try { ctl.enqueue(enc.encode(s)); } catch { cleanup(); } };
          const sendEvent = (e: SaasEvent) => send(`id: ${e.seq}\nevent: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`);
          send(': connected\n\n');
          for (const e of store.events) if (e.seq > (since ?? 0) && match(e)) sendEvent(e);
          const sub = (e: SaasEvent) => { if (match(e)) sendEvent(e); };
          subs.add(sub);
          const hb = setInterval(() => send(':hb\n\n'), Math.max(1, options.pingMs));
          cleanup = () => { clearInterval(hb); subs.delete(sub); closers.delete(cleanup); try { ctl.close(); } catch { /* closed */ } };
          closers.add(cleanup);
        },
        cancel() { cleanup(); },
      });
      return new Response(stream, { status: 200, headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' } });
    } },
  };

  const jsonRes = (status: number, body?: unknown) =>
    status === 204 ? new Response(null, { status }) : new Response(JSON.stringify(body ?? {}), { status, headers: { 'content-type': 'application/json' } });
  const errRes = (e: ApiError) => jsonRes(e.status, { error: { code: e.code, message: e.message, field_errors: e.field_errors ?? {} } });

  api.handle = async (req: Request) => {
    const url = new URL(req.url);
    const m = PREFIX_RE.exec(url.pathname);
    if (!m) return null;
    const name = m[1]!;
    const route = routes[name];
    try {
      if (!route) fail(404, 'not_found', `Unknown method ${name}`);
      if (req.method !== route!.m) fail(405, 'method_not_allowed', `Use ${route!.m}`);
      if (route!.auth && (options.forceUnauthenticated || !store.loggedIn)) fail(401, 'unauthorized', 'Authentication required');
      const flag = EXPERIMENTAL_ROUTE(name);
      if (flag && opts.experimental !== 'all' && !(opts.experimental ?? []).includes(flag)) fail(403, 'experimental_disabled', `Operation is experimental and disabled (enable '${flag}' in saas_core_experimental)`);
      let params: Rec = {};
      let rawBody = '';
      if (route!.m === 'GET') {
        url.searchParams.forEach((v, k) => { params[k] = v; });
        const leid = req.headers.get('last-event-id');
        if (leid) params['Last-Event-ID'] = leid;
      } else {
        rawBody = await req.text();
        if (rawBody) {
          try { const j = JSON.parse(rawBody); if (!j || typeof j !== 'object' || Array.isArray(j)) throw 0; params = j as Rec; }
          catch { fail(400, 'bad_request', 'Body must be a JSON object'); }
        }
      }
      const key = route!.m === 'POST' ? req.headers.get('idempotency-key') : null;
      const ikey = key ? `${name}\u0000${key}` : '';
      if (ikey) {
        const prev = idem.get(ikey);
        if (prev) {
          if (prev.body !== rawBody) fail(409, 'idempotency_mismatch', 'Idempotency-Key was already used with a different request body');
          return jsonRes(prev.status, prev.payload === undefined ? undefined : JSON.parse(prev.payload));
        }
      }
      const out = route!.run(params);
      if (out instanceof Response) return out;
      if (ikey) idem.set(ikey, { body: rawBody, status: out.status, payload: out.body === undefined ? undefined : JSON.stringify(out.body) });
      return jsonRes(out.status, out.body);
    } catch (e) {
      if (e instanceof ApiError) return errRes(e);
      return jsonRes(500, { error: { code: 'internal', message: e instanceof Error ? e.message : 'Internal error', field_errors: {} } });
    }
  };

  api.reset = () => {
    timers.forEach(clearTimeout); timers.clear();
    [...closers].forEach((c) => c());
    subs.clear(); idem.clear(); phases.clear(); seq = 0;
    ({ store, counters } = seed(opts.loggedIn ?? true, opts));
  };
  return api;
}
