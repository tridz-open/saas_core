import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { createClient, type Job, type SaasEvent } from '@frappe-saaskit/client';
import { createMockFetch } from './index.js';
import { createMockServer } from './node.js';

const P = '/api/method/saas_core.api.v1.';
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const mk = (o = {}) => {
  const f = createMockFetch({ stepMs: 0, pingMs: 10, experimental: 'all', ...o });
  const call = async (method: string, path: string, body?: unknown, headers: Record<string, string> = {}) => {
    const isGet = method === 'GET';
    const q = isGet && body ? '?' + new URLSearchParams(body as Record<string, string>).toString() : '';
    const res = await f(`http://x.test${P}${path}${q}`, { method, headers: { 'content-type': 'application/json', ...headers }, body: isGet || body === undefined ? undefined : JSON.stringify(body) });
    const text = await res.text();
    return { status: res.status, res, json: text && res.headers.get('content-type')?.includes('json') ? JSON.parse(text) : undefined };
  };
  const get = (p: string, q?: Record<string, string>) => call('GET', p, q);
  const post = (p: string, b: unknown = {}, h?: Record<string, string>) => call('POST', p, b, h);
  const client = createClient({ baseUrl: 'http://x.test', fetch: f, auth: { type: 'cookie' }, pollIntervalMs: 20 });
  const settle = async (jobId: string) => {
    for (let i = 0; i < 200; i++) { const j = (await get('jobs.get', { job: jobId })).json.message as Job; if (j.state === 'succeeded') return j; await sleep(2); }
    throw new Error('job did not settle');
  };
  return { f, get, post, call, client, settle };
};

describe('public endpoints', () => {
  it('plans.list', async () => {
    const { get } = mk({ loggedIn: false });
    const r = await get('plans.list');
    expect(r.status).toBe(200);
    expect(r.json.message.items.map((p: any) => p.code)).toEqual(['free', 'pro', 'business']);
    expect(r.json.message.next_cursor).toBeNull();
    const y = await get('plans.list', { interval: 'year' });
    expect(y.json.message.items[1].prices).toEqual([{ interval: 'year', amount: 29000, currency: 'USD' }]);
  });
  it('templates.list with cursor/limit', async () => {
    const { get } = mk({ loggedIn: false });
    const a = await get('templates.list', { limit: '2' });
    expect(a.json.message.items).toHaveLength(2);
    const b = await get('templates.list', { limit: '2', cursor: a.json.message.next_cursor });
    expect(b.json.message.items).toHaveLength(1);
    expect(b.json.message.next_cursor).toBeNull();
    expect((await get('templates.list', { limit: '0' })).status).toBe(422);
  });
  it('unknown path returns null from api, wrong method 405', async () => {
    const { f, get } = mk();
    expect(await f.api.handle(new Request('http://x.test/other'))).toBeNull();
    expect((await get('nope')).status).toBe(404);
    expect((await get('signup')).status).toBe(405);
  });
});

describe('auth + session', () => {
  it('signup 201, validation 422, duplicate 409, me includes csrf', async () => {
    const { post, get } = mk({ loggedIn: false });
    expect((await get('me')).status).toBe(401);
    expect((await get('me')).json.error.code).toBe('unauthorized');
    const bad = await post('signup', { email: 'nope', password: 'short' });
    expect(bad.status).toBe(422);
    expect(bad.json.error.field_errors.email).toBeTruthy();
    const ok = await post('signup', { email: 'new@example.com', password: 'a-long-password' });
    expect(ok.status).toBe(201);
    expect(ok.json.message.email).toBe('new@example.com');
    expect((await get('me')).json.message.csrf_token).toBeTruthy();
    expect((await post('signup', { email: 'new@example.com', password: 'a-long-password' })).status).toBe(409);
  });
  it('login / logout', async () => {
    const { post, get } = mk({ loggedIn: false });
    expect((await post('login', { email: 'demo@example.com', password: 'wrong-password' })).status).toBe(401);
    const l = await post('login', { email: 'demo@example.com', password: 'correct-horse-battery' });
    expect(l.status).toBe(200);
    expect(l.json.message.csrf_token).toBeTruthy();
    expect((await get('me')).status).toBe(200);
    expect((await post('logout')).status).toBe(204);
    expect((await get('tenants.list')).status).toBe(401);
  });
  it('forceUnauthenticated', async () => {
    const { get } = mk({ forceUnauthenticated: true });
    expect((await get('tenants.list')).status).toBe(401);
    expect((await get('plans.list')).status).toBe(200);
  });
  it('password reset', async () => {
    const { post } = mk({ loggedIn: false });
    expect((await post('password_reset.request', { email: 'demo@example.com' })).status).toBe(202);
    expect((await post('password_reset.confirm', { key: 'k', new_password: 'short' })).status).toBe(422);
    expect((await post('password_reset.confirm', { key: 'k', new_password: 'new-long-password' })).status).toBe(204);
    expect((await post('login', { email: 'demo@example.com', password: 'new-long-password' })).status).toBe(200);
  });
  it('works through the client', async () => {
    const { client } = mk({ loggedIn: false });
    const me = await client.auth.login({ email: 'demo@example.com', password: 'correct-horse-battery' });
    expect(me.csrf_token).toBeTruthy();
    expect((await client.tenants.list()).length).toBe(2);
    await client.auth.logout();
    await expect(client.me.get()).rejects.toMatchObject({ status: 401 });
  });
});

describe('tenants + jobs', () => {
  it('tenants.list / get / filter / pagination', async () => {
    const { get } = mk();
    const l = await get('tenants.list');
    expect(l.json.message.items.map((t: any) => t.name)).toEqual(['acme', 'legacy-co']);
    expect((await get('tenants.list', { status: 'suspended' })).json.message.items).toHaveLength(1);
    expect((await get('tenants.list', { limit: '1' })).json.message.next_cursor).toBe('1');
    expect((await get('tenants.get', { tenant: 'acme' })).json.message.status).toBe('active');
    expect((await get('tenants.get', { tenant: 'zzz' })).status).toBe(404);
    expect((await get('tenants.get')).status).toBe(422);
    const av = (await get('tenants.slug_availability', { slug: 'acme' })).json.message;
    expect(av).toMatchObject({ slug: 'acme', available: false, reason: 'taken', suggestion: 'acme-2' });
    expect((await get('tenants.slug_availability', { slug: 'admin' })).json.message.reason).toBe('reserved');
    expect((await get('tenants.slug_availability', { slug: 'fresh-co' })).json.message).toMatchObject({ available: true, reason: null, suggestion: null });
  });
  it('tenants.create validates slug, conflicts, then provisions', async () => {
    const { post, get, settle } = mk();
    expect((await post('tenants.create', { slug: 'A!', plan: 'pro' })).json.error.field_errors.slug).toBeTruthy();
    expect((await post('tenants.create', { slug: 'acme', plan: 'pro' })).status).toBe(409);
    expect((await post('tenants.create', { slug: 'widgets', plan: 'nope' })).status).toBe(422);
    expect((await post('tenants.create', { slug: 'widgets', plan: 'free', template: 'crm' })).status).toBe(422);
    const r = await post('tenants.create', { slug: 'widgets', plan: 'pro', template: 'crm' });
    expect(r.status).toBe(202);
    expect(r.json.message.tenant.status).toBe('pending');
    expect(r.json.message.job.state).toBe('pending');
    expect(r.json.message.job.steps.length).toBeGreaterThan(2);
    const job = await settle(r.json.message.job.name);
    expect(job.progress).toBe(1);
    expect(job.steps!.every((s) => s.state === 'succeeded')).toBe(true);
    const t = (await get('tenants.get', { tenant: 'widgets' })).json.message;
    expect(t.status).toBe('active');
    expect(t.url).toContain('widgets');
    expect(t.active_job).toBeNull();
    expect((await get('subscription.get', { tenant: 'widgets' })).json.message.status).toBe('trialing');
  });
  it('job progression passes through every state with progress 0..1', async () => {
    const { post, get, f } = mk({ stepMs: 5 });
    const r = await post('tenants.create', { slug: 'stepper', plan: 'free' });
    const name = r.json.message.job.name;
    const seen = new Set<string>(); const statuses = new Set<string>(); let last = -1;
    for (let i = 0; i < 400 && !seen.has('succeeded'); i++) {
      const j = (await get('jobs.get', { job: name })).json.message as Job;
      seen.add(j.state); expect(j.progress!).toBeGreaterThanOrEqual(last); last = j.progress!;
      statuses.add((await get('tenants.get', { tenant: 'stepper' })).json.message.status);
      await sleep(2);
    }
    expect([...seen]).toEqual(expect.arrayContaining(['pending', 'dispatching', 'submitted', 'running', 'succeeded']));
    expect([...statuses]).toEqual(expect.arrayContaining(['provisioning', 'active']));
    f.reset();
  });
  it('removed endpoints are 404', async () => {
    const { post } = mk();
    for (const n of ['tenants.duplicate', 'subscription.cancel', 'subscription.portal_link']) expect((await post(n, { tenant: 'acme' })).status).toBe(404);
  });
  it('experimental routes are 403 experimental_disabled by default and open when enabled', async () => {
    const off = mk({ experimental: undefined });
    for (const [m, n] of [['GET', 'api_tokens.list'], ['GET', 'tenants.team.list'], ['GET', 'support.tickets.list'], ['GET', 'events.stream'], ['POST', 'tenants.backups.restore'], ['POST', 'tenants.backups.download_link']] as const) {
      const r = m === 'GET' ? await off.get(n, { tenant: 'acme' }) : await off.post(n, { tenant: 'acme' });
      expect([n, r.status, r.json.error.code]).toEqual([n, 403, 'experimental_disabled']);
    }
    expect((await off.get('plans.list')).status).toBe(200);
    expect((await mk({ experimental: ['api_tokens'] }).get('api_tokens.list')).status).toBe(200);
  });
  it('tenants.request_delete', async () => {
    const { post, get, settle } = mk();
    expect((await post('tenants.request_delete', { tenant: 'acme', confirm_slug: 'wrong' })).status).toBe(422);
    const r = await post('tenants.request_delete', { tenant: 'acme', confirm_slug: 'acme', reason: 'bye' });
    expect(r.status).toBe(202);
    expect(r.json.message.kind).toBe('terminate');
    expect((await get('tenants.get', { tenant: 'acme' })).json.message.status).toBe('terminating');
    expect((await post('tenants.backups.create', { tenant: 'acme' })).status).toBe(409);
    await settle(r.json.message.name);
    expect((await get('tenants.get', { tenant: 'acme' })).status).toBe(404);
  });
  it('tenants.suspend_status', async () => {
    const { get } = mk();
    expect((await get('tenants.suspend_status', { tenant: 'acme' })).json.message.suspended).toBe(false);
    const s = (await get('tenants.suspend_status', { tenant: 'legacy-co' })).json.message;
    expect(s).toMatchObject({ suspended: true, reason: 'dunning', resume_by_paying: true });
  });
  it('tenants.open_login_link', async () => {
    const { post } = mk();
    const r = await post('tenants.open_login_link', { tenant: 'acme' });
    expect(r.json.message.url).toMatch(/^https:\/\/acme\./);
    expect(Date.parse(r.json.message.expires_at)).toBeGreaterThan(Date.now());
    expect((await post('tenants.open_login_link', { tenant: 'legacy-co' })).status).toBe(409);
  });
  it('tenants.change_plan', async () => {
    const { post, get, settle } = mk();
    expect((await post('tenants.change_plan', { tenant: 'acme', plan: 'pro' })).status).toBe(409);
    expect((await post('tenants.change_plan', { tenant: 'acme', plan: 'zzz' })).status).toBe(422);
    const r = await post('tenants.change_plan', { tenant: 'acme', plan: 'business' });
    expect(r.status).toBe(202);
    await settle(r.json.message.name);
    expect((await get('tenants.get', { tenant: 'acme' })).json.message.plan).toBe('business');
    expect((await get('subscription.get', { tenant: 'acme' })).json.message.plan).toBe('business');
  });
  it('jobs.get / jobs.list', async () => {
    const { get } = mk();
    expect((await get('jobs.get', { job: 'JOB-0001' })).json.message.steps).toHaveLength(5);
    expect((await get('jobs.get', { job: 'nope' })).status).toBe(404);
    expect((await get('jobs.list', { tenant: 'acme' })).json.message.items).toHaveLength(1);
    expect((await get('jobs.list')).json.message.items).toHaveLength(2);
  });
});

describe('backups', () => {
  it('list, create, download_link', async () => {
    const { get, post, settle } = mk();
    const l = await get('tenants.backups.list', { tenant: 'acme' });
    expect(l.json.message.items).toHaveLength(2);
    const r = await post('tenants.backups.create', { tenant: 'acme' });
    expect(r.status).toBe(202);
    expect(r.json.message.kind).toBe('backup');
    await settle(r.json.message.name);
    const after = (await get('tenants.backups.list', { tenant: 'acme' })).json.message.items;
    expect(after).toHaveLength(3);
    const d = await post('tenants.backups.download_link', { tenant: 'acme', backup: after[0].name, part: 'database' });
    expect(d.json.message.url).toContain('database');
    expect((await post('tenants.backups.download_link', { tenant: 'acme', backup: 'zz', part: 'database' })).status).toBe(404);
    expect((await post('tenants.backups.download_link', { tenant: 'acme', backup: after[0].name, part: 'x' })).status).toBe(422);
  });
});

describe('billing', () => {
  it('checkout.create_session', async () => {
    const { post, get, settle } = mk();
    const body = { slug: 'shop-one', plan: 'pro', interval: 'month', success_url: 'https://a.test/ok', cancel_url: 'https://a.test/no' };
    expect((await post('checkout.create_session', { ...body, success_url: 'bad' })).status).toBe(422);
    expect((await post('checkout.create_session', { ...body, slug: 'acme' })).status).toBe(409);
    const r = await post('checkout.create_session', body);
    expect(r.status).toBe(201);
    expect(r.json.message).toMatchObject({ gateway: 'razorpay', tenant: 'shop-one' });
    expect(r.json.message.session_id).toBeTruthy();
    expect(r.json.message.url).toMatch(/^https:/);
    const t = (await get('tenants.get', { tenant: 'shop-one' })).json.message;
    await settle(t.active_job);
  });
  it('subscription get; billing preview_cancel / cancel / preview_change', async () => {
    const { get, post } = mk();
    expect((await get('subscription.get', { tenant: 'acme' })).json.message.status).toBe('active');
    const pv = (await get('billing.preview_cancel', { tenant: 'acme' })).json.message;
    expect(pv.can_cancel).toBe(true);
    const c = await post('billing.cancel', { tenant: 'acme', reason: 'x' });
    expect(c.status).toBe(200);
    expect(c.json.message.status).toBe('canceling');
    expect((await post('billing.cancel', { tenant: 'acme' })).status).toBe(409);
    const ch = (await get('billing.preview_change', { tenant: 'acme', plan: 'business' })).json.message;
    expect(ch).toMatchObject({ from_plan: 'pro', to_plan: 'business', direction: 'upgrade' });
  });
  it('invoices list/get/pdf', async () => {
    const { get, f } = mk();
    expect((await get('invoices.list')).json.message.items).toHaveLength(4);
    const acme = (await get('invoices.list', { tenant: 'acme' })).json.message.items;
    expect(acme).toHaveLength(3);
    expect(acme[0].has_pdf).toBe(true);
    expect((await get('invoices.get', { invoice: acme[0].name })).json.message.total).toEqual({ amount: 2900, currency: 'USD' });
    expect((await get('invoices.get', { invoice: 'x' })).status).toBe(404);
    const res = await f(`http://x.test${P}invoices.pdf?invoice=${acme[0].name}`);
    expect(res.headers.get('content-type')).toBe('application/pdf');
    expect(new TextDecoder().decode((await res.arrayBuffer()).slice(0, 5))).toBe('%PDF-');
  });
  it('payment_methods.portal_link', async () => {
    const { post } = mk();
    expect((await post('payment_methods.portal_link', { tenant: 'acme' })).json.message.expires_at).toBeTruthy();
    expect((await post('payment_methods.portal_link', {})).status).toBe(200);
  });
});

describe('support', () => {
  it('list / get / create / reply', async () => {
    const { get, post } = mk();
    expect((await get('support.tickets.list')).json.message.items).toHaveLength(2);
    expect((await get('support.tickets.get', { ticket: 'TKT-0001' })).json.message.replies).toHaveLength(2);
    expect((await get('support.tickets.get', { ticket: 'nope' })).status).toBe(404);
    expect((await post('support.tickets.create', { subject: '', body: 'x' })).status).toBe(422);
    const c = await post('support.tickets.create', { tenant: 'acme', subject: 'Help', body: 'Broken' });
    expect(c.status).toBe(201);
    const r = await post('support.tickets.reply', { ticket: c.json.message.name, body: 'More info' });
    expect(r.status).toBe(200);
    expect(r.json.message.replies).toHaveLength(2);
  });
});

describe('idempotency', () => {
  it('same key+body replays, different body 409', async () => {
    const { post, get } = mk();
    const h = { 'Idempotency-Key': 'k1' };
    const a = await post('tenants.create', { slug: 'idem-one', plan: 'free' }, h);
    const b = await post('tenants.create', { slug: 'idem-one', plan: 'free' }, h);
    expect(b.status).toBe(202);
    expect(b.json).toEqual(a.json);
    expect((await get('tenants.list')).json.message.items).toHaveLength(3);
    const c = await post('tenants.create', { slug: 'idem-two', plan: 'free' }, h);
    expect(c.status).toBe(409);
    expect(c.json.error.code).toBe('idempotency_mismatch');
  });
});

describe('events.stream (SSE)', () => {
  async function collect(f: typeof fetch, query: string, until: (e: SaasEvent) => boolean, headers: Record<string, string> = {}) {
    const ctl = new AbortController();
    const res = await f(`http://x.test${P}events.stream?${query}`, { headers: { accept: 'text/event-stream', ...headers }, signal: ctl.signal });
    expect(res.headers.get('content-type')).toContain('text/event-stream');
    const reader = res.body!.getReader(); const dec = new TextDecoder();
    let buf = '', raw = ''; const events: SaasEvent[] = [];
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value); raw += dec.decode(value);
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        const data = block.split('\n').find((l) => l.startsWith('data: '));
        if (data) { const e = JSON.parse(data.slice(6)) as SaasEvent; events.push(e); if (until(e)) { ctl.abort(); await reader.cancel().catch(() => {}); return { events, raw }; } }
      }
    }
    return { events, raw };
  }
  it('streams ordered job + tenant events with SSE framing and replay', async () => {
    const { post, f } = mk({ stepMs: 5 });
    const r = await post('tenants.create', { slug: 'live-one', plan: 'free' });
    const job = r.json.message.job.name;
    const { events, raw } = await collect(f, `job=${job}`, (e) => e.type === 'job.updated' && (e.data as any).state === 'succeeded');
    expect(raw).toMatch(/id: \d+\nevent: job\.(updated|step)\ndata: \{/);
    const seqs = events.map((e) => e.seq);
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
    expect(new Set(seqs).size).toBe(seqs.length);
    expect(events.some((e) => e.type === 'job.step')).toBe(true);
    expect(events.every((e) => e.job === job)).toBe(true);
    // tenant stream carries the job events (no tenant.updated any more)
    const t = await collect(f, 'tenant=live-one', (e) => e.type === 'job.updated' && (e.data as any).state === 'succeeded');
    expect(t.events.every((e) => e.type !== ('tenant.updated' as string))).toBe(true);
    // since / Last-Event-ID replay
    const after = events[2]!.seq;
    const rep = await collect(f, `job=${job}&since=${after}`, (e) => e.type === 'job.updated' && (e.data as any).state === 'succeeded');
    expect(rep.events[0]!.seq).toBeGreaterThan(after);
    const rep2 = await collect(f, `job=${job}`, (e) => e.type === 'job.updated' && (e.data as any).state === 'succeeded', { 'Last-Event-ID': String(after) });
    expect(rep2.events[0]!.seq).toBe(rep.events[0]!.seq);
  });
  it('sends heartbeats', async () => {
    const { f } = mk({ pingMs: 5 });
    const ctl = new AbortController();
    const res = await f(`http://x.test${P}events.stream?tenant=acme`, { signal: ctl.signal });
    const reader = res.body!.getReader(); let raw = '';
    for (let i = 0; i < 20 && !raw.includes(':hb'); i++) raw += new TextDecoder().decode((await reader.read()).value);
    expect(raw).toContain(':hb');
    await reader.cancel();
  });
  it('requires auth', async () => {
    const { get } = mk({ loggedIn: false });
    expect((await get('events.stream')).status).toBe(401);
  });
  it('client.subscribe receives terminal job.updated', async () => {
    const { client, post } = mk({ stepMs: 3 });
    const r = await client.tenants.create({ slug: 'sub-one', plan: 'free' });
    const got: SaasEvent[] = [];
    await new Promise<void>((resolve, reject) => {
      client.subscribe({ jobId: r.job.name }, (e) => { got.push(e); if (e.type === 'job.updated' && (e.data as any).state === 'succeeded') resolve(); }, reject);
      setTimeout(() => reject(new Error('timeout')), 4000);
    });
    expect(got.length).toBeGreaterThan(1);
    void post;
  });
});

describe('msw handlers', () => {
  const server = createMockServer({ stepMs: 0 });
  beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
  afterEach(() => server.resetStore());
  afterAll(() => server.close());
  it('serves any baseUrl prefix', async () => {
    for (const base of ['http://api.test', 'https://other.example.com/proxy']) {
      const c = createClient({ baseUrl: base, auth: { type: 'cookie' } });
      expect((await c.plans.list()).length).toBe(3);
      expect((await c.tenants.list()).length).toBe(2);
    }
  });
});

describe('profile, tokens, team, restore', () => {
  it('me.update name and password', async () => {
    const { post } = mk();
    const a = await post('me.update', { full_name: 'New Name' });
    expect(a.status).toBe(200);
    expect(a.json.message.full_name).toBe('New Name');
    const bad = await post('me.update', { current_password: 'wrong', new_password: 'longenough123' });
    expect(bad.status).toBe(422);
    expect(bad.json.error.code).toBe('validation_error');
    expect(bad.json.error.field_errors.current_password).toBeTruthy();
    expect((await post('me.update', { current_password: 'correct-horse-battery', new_password: 'short' })).status).toBe(422);
    expect((await post('me.update', { current_password: 'correct-horse-battery', new_password: 'longenough123' })).status).toBe(200);
    expect((await post('login', { email: 'demo@example.com', password: 'longenough123' })).status).toBe(200);
  });
  it('api tokens list/create/revoke', async () => {
    const { get, post } = mk();
    const l = await get('api_tokens.list');
    expect(l.json.message.items).toHaveLength(2);
    expect(l.json.message.next_cursor).toBeNull();
    const c = await post('api_tokens.create', { label: 'ci' });
    expect(c.status).toBe(201);
    expect(c.json.message.secret).toBeTruthy();
    expect(c.json.message.api_key).toBeTruthy();
    expect(c.json.message.token.secret).toBeUndefined();
    const after = (await get('api_tokens.list')).json.message.items;
    expect(after).toHaveLength(3);
    expect(JSON.stringify(after)).not.toContain(c.json.message.secret);
    expect((await post('api_tokens.create', {})).status).toBe(422);
    const r = await post('api_tokens.revoke', { token: c.json.message.token.name });
    expect(r.status).toBe(204);
    expect((await post('api_tokens.revoke', { token: c.json.message.token.name })).status).toBe(404);
    expect((await get('api_tokens.list')).json.message.items).toHaveLength(2);
  });
  it('team lifecycle', async () => {
    const { get, post } = mk();
    const l = await get('tenants.team.list', { tenant: 'acme' });
    expect(l.json.message.items).toHaveLength(1);
    const owner = l.json.message.items[0];
    expect(owner.role).toBe('owner');
    const inv = await post('tenants.team.invite', { tenant: 'acme', email: 'bob@example.com', role: 'member' });
    expect(inv.status).toBe(201);
    expect(inv.json.message.status).toBe('invited');
    const dup = await post('tenants.team.invite', { tenant: 'acme', email: 'bob@example.com', role: 'admin' });
    expect(dup.status).toBe(409);
    expect(dup.json.error.code).toBe('conflict');
    expect((await post('tenants.team.invite', { tenant: 'nope', email: 'x@example.com', role: 'member' })).status).toBe(404);
    const ch = await post('tenants.team.change_role', { tenant: 'acme', member: inv.json.message.name, role: 'admin' });
    expect(ch.status).toBe(200);
    expect(ch.json.message.role).toBe('admin');
    expect((await post('tenants.team.change_role', { tenant: 'acme', member: owner.name, role: 'member' })).status).toBe(403);
    expect((await post('tenants.team.remove', { tenant: 'acme', member: owner.name })).status).toBe(403);
    const acc = await post('team.accept_invite', { token: `invite-${inv.json.message.name}` });
    expect(acc.status).toBe(200);
    expect(acc.json.message.status).toBe('active');
    expect((await post('team.accept_invite', { token: 'bogus' })).status).toBe(404);
    expect((await post('tenants.team.remove', { tenant: 'acme', member: inv.json.message.name })).status).toBe(204);
    const inv2 = await post('tenants.team.invite', { tenant: 'acme', email: 'c@example.com', role: 'viewer' });
    expect((await post('tenants.team.revoke_invite', { tenant: 'acme', invite: inv2.json.message.name })).status).toBe(204);
    expect((await get('tenants.team.list', { tenant: 'acme' })).json.message.items).toHaveLength(1);
  });
  it('backups.restore is approval-only and never creates a job', async () => {
    const { get, post } = mk();
    const before = (await get('jobs.list')).json.message.items.length;
    const r = await post('tenants.backups.restore', { tenant: 'acme', backup: 'bk-acme-0001' });
    expect(r.status).toBe(202);
    expect(r.json.message.status).toBe('pending_approval');
    expect(r.json.message.approval_request).toBeTruthy();
    expect((await post('tenants.backups.restore', { tenant: 'acme', backup: 'nope' })).status).toBe(404);
    expect((await get('jobs.list')).json.message.items.length).toBe(before);
  });
});

describe('v1.2 additions', () => {
  it('verify_email.request returns 202', async () => {
    const { post } = mk();
    expect((await post('verify_email.request')).status).toBe(202);
  });
  it('verify_email.request requires auth', async () => {
    const { post } = mk({ loggedIn: false });
    expect((await post('verify_email.request')).status).toBe(401);
  });
  it('verify_email.confirm returns 204 with key, 422 without', async () => {
    const { post } = mk();
    expect((await post('verify_email.confirm', { key: 'k1' })).status).toBe(204);
    expect((await post('verify_email.confirm', {})).status).toBe(422);
  });
  it('support.tickets.close sets status closed and persists', async () => {
    const { post, get } = mk();
    const c = await post('support.tickets.create', { tenant: 'acme', subject: 'Help', body: 'Broken' });
    const id = c.json.message.name;
    const r = await post('support.tickets.close', { ticket: id });
    expect(r.status).toBe(200);
    expect(r.json.message.status).toBe('closed');
    expect((await get('support.tickets.get', { ticket: id })).json.message.status).toBe('closed');
  });
  it('support.tickets.close 404 on unknown ticket; reply to closed is 409', async () => {
    const { post } = mk();
    expect((await post('support.tickets.close', { ticket: 'nope' })).status).toBe(404);
    const c = await post('support.tickets.create', { tenant: 'acme', subject: 'Help', body: 'x' });
    await post('support.tickets.close', { ticket: c.json.message.name });
    expect((await post('support.tickets.reply', { ticket: c.json.message.name, body: 'again' })).status).toBe(409);
  });
  it('client round-trip: close ticket', async () => {
    const { client } = mk();
    const t = await client.support.create({ tenant: 'acme', subject: 'S', body: 'B' } as any);
    expect((await client.support.close(t.name)).status).toBe('closed');
    await expect(client.auth.requestEmailVerification()).resolves.toBeUndefined();
    await expect(client.auth.confirmEmail('k')).resolves.toBeUndefined();
  });
});

describe('v1.3 additions', () => {
  it('config is public and reflects options', async () => {
    const a = mk({ loggedIn: false });
    const r = await a.get('config');
    expect(r.status).toBe(200);
    expect(r.json.message.public_signup).toBe(true);
    expect(r.json.message.payment_banner).toBeTruthy();
    const b = mk({ publicSignup: false, paymentBanner: null });
    expect((await b.get('config')).json.message).toEqual({ public_signup: false, payment_banner: null, site_domain: 'mock.example.com' });
  });
  it('signup returns 403 signup_disabled when public signup is off', async () => {
    const { post } = mk({ loggedIn: false, publicSignup: false });
    const r = await post('signup', { email: 'new@example.com', password: 'longenough123' });
    expect(r.status).toBe(403);
    expect(r.json.error.code).toBe('signup_disabled');
  });
  it('subscription and invoices carry payment_mocked', async () => {
    const { get } = mk();
    expect((await get('subscription.get', { tenant: 'acme' })).json.message.payment_mocked).toBe(true);
    expect((await get('invoices.list')).json.message.items[0].payment_mocked).toBe(true);
  });
  it('admin.tenants.create provisions, derives slug, lists, 409 on duplicate', async () => {
    const { post, get } = mk();
    const r = await post('admin.tenants.create', { owner_email: 'boss@example.com', plan: 'pro', name: 'Zed Corp' });
    expect(r.status).toBe(202);
    expect(r.json.message.tenant.slug).toBe('zed-corp');
    expect(r.json.message.job.kind).toBe('provision');
    const l = await get('admin.tenants.list', { owner_email: 'boss@example.com' });
    expect(l.json.message.items.map((t: any) => t.slug)).toEqual(['zed-corp']);
    expect((await post('admin.tenants.create', { owner_email: 'boss@example.com', plan: 'pro', subdomain: 'zed-corp' })).status).toBe(409);
  });
  it('admin.tenants.create validates and signals capacity_exhausted', async () => {
    const { post } = mk();
    expect((await post('admin.tenants.create', { owner_email: 'bad', plan: 'pro' })).status).toBe(422);
    expect((await post('admin.tenants.create', { owner_email: 'a@example.com', plan: 'nope' })).status).toBe(422);
    const full = mk({ capacityExhausted: true });
    const r = await full.post('admin.tenants.create', { owner_email: 'a@example.com', plan: 'pro', subdomain: 'fullcap' });
    expect(r.status).toBe(503);
    expect(r.json.error.code).toBe('capacity_exhausted');
  });
  it('admin.tenants.invite adds an owner invite; 404 unknown tenant; 409 duplicate', async () => {
    const { post } = mk();
    const r = await post('admin.tenants.invite', { tenant: 'acme', owner_email: 'x@example.com' });
    expect(r.status).toBe(201);
    expect(r.json.message.role).toBe('owner');
    expect(r.json.message.status).toBe('invited');
    expect((await post('admin.tenants.invite', { tenant: 'acme', owner_email: 'x@example.com' })).status).toBe(409);
    expect((await post('admin.tenants.invite', { tenant: 'nope', owner_email: 'y@example.com' })).status).toBe(404);
  });
  it('admin endpoints 403 without SaaS Admin, 401 when logged out', async () => {
    const na = mk({ admin: false });
    expect((await na.get('admin.tenants.list')).status).toBe(403);
    expect((await na.post('admin.tenants.create', { owner_email: 'a@example.com', plan: 'pro' })).status).toBe(403);
    expect((await mk({ loggedIn: false }).get('admin.tenants.list')).status).toBe(401);
  });
  it('client round-trip: config + admin', async () => {
    const { client } = mk();
    expect((await client.config.get()).public_signup).toBe(true);
    const r = await client.admin.tenants.create({ owner_email: 'rt@example.com', plan: 'pro', subdomain: 'roundtrip' });
    expect(r.tenant.slug).toBe('roundtrip');
    expect((await client.admin.tenants.list()).some((t) => t.slug === 'roundtrip')).toBe(true);
  });
});

describe('manual payment + signup modes', () => {
  const body = { plan: 'pro', interval: 'month', slug: 'manualco', success_url: 'https://a.test/ok', cancel_url: 'https://a.test/no' };
  it('createSession returns manual client_payload; verifyPayment ok then duplicate', async () => {
    const { client } = mk();
    const s: any = await client.checkout.createSession(body as any);
    expect(s.client_payload).toEqual({ provider: 'manual' });
    expect(s.payment_mocked).toBe(true);
    const a = await client.checkout.verifyPayment('manual', { session_id: s.session_id });
    expect(a).toEqual({ status: 'ok', tenant: s.tenant });
    expect(await client.checkout.verifyPayment('manual', { session_id: s.session_id })).toEqual({ status: 'duplicate', tenant: s.tenant });
  });
  it('signup modes return 403 codes', async () => {
    const cred = { email: 'a@example.com', password: 'a-long-password' };
    const code = async (o: object, b: object = cred) => { const r = await mk({ loggedIn: false, ...o }).post('signup', b); return [r.status, r.json.error.code]; };
    expect(await code({ signupMode: 'disabled' })).toEqual([403, 'signup_disabled']);
    expect(await code({ signupMode: 'allowlist', signupAllowlist: ['x@example.com'] })).toEqual([403, 'not_allowlisted']);
    expect(await code({ signupMode: 'invite', signupInvites: ['t1'] })).toEqual([403, 'invite_required']);
    expect((await mk({ loggedIn: false, signupMode: 'allowlist', signupAllowlist: ['A@example.com'] }).post('signup', cred)).status).toBe(201);
    expect((await mk({ loggedIn: false, signupMode: 'invite', signupInvites: ['t1'] }).post('signup', { ...cred, invite_token: 't1' })).status).toBe(201);
  });
});
