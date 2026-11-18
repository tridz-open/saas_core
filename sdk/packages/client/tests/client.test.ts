import { describe, it, expect, vi } from 'vitest';
import { createClient, AuthError, ExperimentalDisabledError, ForbiddenError, NotFoundError, ValidationError, ConflictError, RateLimitError, ServerError, NetworkError } from '../src/index';
import { createSseParser } from '../src/sse';

const P = 'http://x/api/method/saas_core.api.v1.';
const ok = (payload: unknown, status = 200, h: Record<string, string> = {}) => new Response(JSON.stringify({ message: payload }), { status, headers: h });
const err = (status: number, error: object = { code: 'c', message: 'm', field_errors: { a: 'bad' } }, h: Record<string, string> = {}) => new Response(JSON.stringify({ error }), { status, headers: h });
const mk = (fetch: any, extra: any = {}) => createClient({ baseUrl: 'http://x/', fetch, retry: { retries: 2, baseDelayMs: 1, maxDelayMs: 2 }, ...extra });
const sse = (text: string) => new Response(new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(text)); c.close(); } }), { status: 200 });
const ev = (type: string, data: object, extra: object = {}) => ({ seq: 1, type, at: 't', ...extra, data });
const call = (f: any, i: number) => ({ url: f.mock.calls[i][0] as string, init: f.mock.calls[i][1] as RequestInit & { headers: Headers } });
const bodyOf = (f: any, i: number) => JSON.parse(call(f, i).init.body as string);

describe('auth strategies', () => {
  it('cookie uses credentials include and contract path', async () => {
    const f = vi.fn(async () => ok({ items: [], next_cursor: null }));
    await mk(f, { auth: { type: 'cookie' } }).plans.list();
    expect(call(f, 0).url).toBe(`${P}plans.list`);
    expect(call(f, 0).init.credentials).toBe('include');
  });
  it('apiKey default and custom header', async () => {
    const f = vi.fn(async () => ok({ items: [], next_cursor: null }));
    await mk(f, { auth: { type: 'apiKey', key: 'k:s' } }).plans.list();
    expect(call(f, 0).init.headers.get('Authorization')).toBe('token k:s');
    await mk(f, { auth: { type: 'apiKey', key: 'abc', header: 'X-Key' } }).plans.list();
    expect(call(f, 1).init.headers.get('X-Key')).toBe('abc');
  });
  it('CSRF header sent after login with cookie auth, cleared on logout', async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(ok({ email: 'guest' })) // bootstrap me: guest session, no token
      .mockResolvedValueOnce(ok({ email: 'a', csrf_token: 'CSRF1' }))
      .mockResolvedValueOnce(ok({}))
      .mockResolvedValueOnce(ok(null))
      .mockResolvedValueOnce(ok({}))
      .mockResolvedValueOnce(ok({}));
    const c = mk(f, { auth: { type: 'cookie' } });
    await c.auth.login({ email: 'a', password: 'p' } as any);
    expect(call(f, 1).init.headers.get('X-Frappe-CSRF-Token')).toBeNull();
    await c.tenants.openLoginLink('t1');
    expect(call(f, 2).init.headers.get('X-Frappe-CSRF-Token')).toBe('CSRF1');
    await c.auth.logout();
    expect(call(f, 3).init.headers.get('X-Frappe-CSRF-Token')).toBe('CSRF1');
    await c.tenants.openLoginLink('t1');
    expect(call(f, 5).init.headers.get('X-Frappe-CSRF-Token')).toBeNull();
  });
  it('CSRF not sent on GET or non-cookie auth', async () => {
    const f = vi.fn(async () => ok({ email: 'a', csrf_token: 'C' }));
    const c = mk(f, { auth: { type: 'apiKey', key: 't' } });
    await c.auth.login({ email: 'a', password: 'p' } as any);
    await c.auth.login({ email: 'a', password: 'p' } as any);
    expect(call(f, 1).init.headers.get('X-Frappe-CSRF-Token')).toBeNull();
  });
});

describe('errors', () => {
  const cases: [number, any][] = [[401, AuthError], [403, ForbiddenError], [404, NotFoundError], [409, ConflictError], [422, ValidationError]];
  for (const [s, C] of cases) it(`${s} -> ${C.name}`, async () => {
    const f = vi.fn(async () => err(s));
    const e = await mk(f).me.get().catch((x) => x);
    expect(e).toBeInstanceOf(C);
    expect(e).toMatchObject({ status: s, code: 'c', message: 'm', fieldErrors: { a: 'bad' } });
    expect(f).toHaveBeenCalledTimes(1);
  });
  it('unwraps Frappe {message:{error}} envelope so err.code is the server code', async () => {
    const f = vi.fn(async () => new Response(JSON.stringify({ message: { error: { code: 'signup_disabled', message: 'Signup is currently closed' } } }), { status: 403 }));
    const e = await mk(f).me.get().catch((x) => x);
    expect(e).toBeInstanceOf(ForbiddenError);
    expect(e).toMatchObject({ status: 403, code: 'signup_disabled', message: 'Signup is currently closed' });
  });
  it('maps 403 experimental_disabled to ExperimentalDisabledError (a ForbiddenError)', async () => {
    const f = vi.fn(async () => err(403, { code: 'experimental_disabled', message: 'disabled' }));
    const e = await mk(f).apiTokens.list().catch((x) => x);
    expect(e).toBeInstanceOf(ExperimentalDisabledError);
    expect(e).toBeInstanceOf(ForbiddenError);
    expect(e).toMatchObject({ status: 403, code: 'experimental_disabled' });
  });
  it('falls back to polling right away when the SSE stream is experimental_disabled', async () => {
    const f = vi.fn(async (url: string) => (url.includes('events.stream') ? err(403, { code: 'experimental_disabled', message: 'x' }) : ok({ name: 'j1', state: 'succeeded' })));
    const evs: any[] = [];
    mk(f, { pollIntervalMs: 5 }).subscribe({ jobId: 'j1' }, (e) => evs.push(e));
    await vi.waitFor(() => expect(evs.length).toBe(1));
    expect(f.mock.calls.filter((c: any) => c[0].includes('events.stream')).length).toBe(1);
  });
  it('429 with Retry-After retries then throws', async () => {
    const f = vi.fn(async () => err(429, { message: 'slow' }, { 'Retry-After': '0' }));
    const e = await mk(f).me.get().catch((x) => x);
    expect(e).toBeInstanceOf(RateLimitError);
    expect(e.retryAfterMs).toBe(0);
    expect(f).toHaveBeenCalledTimes(3);
  });
  it('5xx -> ServerError, fetch failure -> NetworkError', async () => {
    expect(await mk(async () => err(503)).me.get().catch((x) => x)).toBeInstanceOf(ServerError);
    expect(await mk(async () => { throw new TypeError('boom'); }).me.get().catch((x) => x)).toBeInstanceOf(NetworkError);
  });
});

describe('retry + idempotency', () => {
  it('reuses key across retries; GET has none', async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(err(500))
      .mockRejectedValueOnce(new TypeError('x'))
      .mockResolvedValueOnce(ok({ tenant: { name: 't' }, job: { name: 'j' } }));
    const r: any = await mk(f).tenants.create({ name: 'n', slug: 's', plan: 'p' } as any);
    expect(r.job.name).toBe('j');
    const keys = f.mock.calls.map((c: any) => c[1].headers.get('Idempotency-Key'));
    expect(keys[0]).toBeTruthy();
    expect(new Set(keys).size).toBe(1);
    const g = vi.fn(async () => ok({ items: [], next_cursor: null }));
    await mk(g).plans.list();
    expect(call(g, 0).init.headers.get('Idempotency-Key')).toBeNull();
  });
  it('new key per call, POST JSON body', async () => {
    const f = vi.fn(async () => ok({ name: 'j' }));
    const c = mk(f);
    await c.tenants.changePlan('a', 'p'); await c.tenants.changePlan('a', 'p');
    expect(call(f, 0).init.method).toBe('POST');
    expect(call(f, 0).init.headers.get('Content-Type')).toBe('application/json');
    expect(call(f, 0).init.headers.get('Idempotency-Key')).not.toBe(call(f, 1).init.headers.get('Idempotency-Key'));
  });
});

describe('methods', () => {
  const setup = (payload: unknown = {}) => { const f = vi.fn(async () => ok(payload)); return { f, c: mk(f) }; };

  it('auth.signup/login return Me; logout; password reset', async () => {
    const { f, c } = setup({ email: 'a', csrf_token: 'x' });
    expect((await c.auth.signup({ email: 'a', password: 'p' } as any)).email).toBe('a');
    expect(call(f, 0).url).toBe(`${P}signup`);
    expect(bodyOf(f, 0)).toEqual({ email: 'a', password: 'p' });
    await c.auth.login({ email: 'a', password: 'p' } as any);
    expect(call(f, 1).url).toBe(`${P}login`);
    await c.auth.logout();
    expect(call(f, 2).url).toBe(`${P}logout`);
    await c.auth.requestPasswordReset('a@b.c');
    expect(call(f, 3).url).toBe(`${P}password_reset.request`);
    expect(bodyOf(f, 3)).toEqual({ email: 'a@b.c' });
    await c.auth.confirmPasswordReset({ key: 'k', new_password: 'n' });
    expect(call(f, 4).url).toBe(`${P}password_reset.confirm`);
    expect(bodyOf(f, 4)).toEqual({ key: 'k', new_password: 'n' });
  });

  it('list unwraps items; listPage returns page; GET uses query params', async () => {
    const { f, c } = setup({ items: [{ name: 't1' }], next_cursor: 'n' });
    expect(await c.tenants.list({ status: 'active', limit: 5 })).toEqual([{ name: 't1' }]);
    const u = new URL(call(f, 0).url);
    expect(u.pathname).toBe('/api/method/saas_core.api.v1.tenants.list');
    expect(u.searchParams.get('status')).toBe('active');
    expect(u.searchParams.get('limit')).toBe('5');
    expect(call(f, 0).init.method).toBe('GET');
    expect(call(f, 0).init.body).toBeUndefined();
    expect(await c.tenants.listPage()).toEqual({ items: [{ name: 't1' }], next_cursor: 'n' });
  });

  it('tenants.get, suspendStatus, openLoginLink, requestDelete, changePlan', async () => {
    const { f, c } = setup({ x: 1 });
    await c.tenants.get('t1');
    expect(call(f, 0).url).toBe(`${P}tenants.get?tenant=t1`);
    await c.tenants.suspendStatus('t1');
    expect(call(f, 1).url).toBe(`${P}tenants.suspend_status?tenant=t1`);
    await c.tenants.openLoginLink('t1');
    expect(call(f, 2).url).toBe(`${P}tenants.open_login_link`);
    expect(bodyOf(f, 2)).toEqual({ tenant: 't1' });
    await c.tenants.requestDelete('t1', { confirm_slug: 's', reason: 'r' });
    expect(call(f, 3).url).toBe(`${P}tenants.request_delete`);
    expect(bodyOf(f, 3)).toEqual({ tenant: 't1', confirm_slug: 's', reason: 'r' });
    await c.tenants.changePlan('t1', 'pro');
    expect(call(f, 4).url).toBe(`${P}tenants.change_plan`);
    expect(bodyOf(f, 4)).toEqual({ tenant: 't1', plan: 'pro' });
  });

  it('backups list/create/downloadLink', async () => {
    const { f, c } = setup({ items: [], next_cursor: null });
    await c.backups.list('t1');
    expect(call(f, 0).url).toBe(`${P}tenants.backups.list?tenant=t1`);
    await c.backups.create('t1');
    expect(call(f, 1).url).toBe(`${P}tenants.backups.create`);
    expect(bodyOf(f, 1)).toEqual({ tenant: 't1' });
    await c.backups.downloadLink('t1', 'b1', 'database');
    expect(call(f, 2).url).toBe(`${P}tenants.backups.download_link`);
    expect(bodyOf(f, 2)).toEqual({ tenant: 't1', backup: 'b1', part: 'database' });
  });

  it('checkout.createSession', async () => {
    const { f, c } = setup({ url: 'u' });
    await c.checkout.createSession({ plan: 'p' } as any);
    expect(call(f, 0).url).toBe(`${P}checkout.create_session`);
    expect(bodyOf(f, 0)).toEqual({ plan: 'p' });
  });

  it('checkout.verifyPayment', async () => {
    const { f, c } = setup({ status: 'ok' });
    const r = await c.checkout.verifyPayment('razorpay', { razorpay_payment_id: 'p', razorpay_signature: 's' });
    expect(call(f, 0).url).toBe(`${P}checkout.verify_payment`);
    expect(bodyOf(f, 0)).toEqual({ gateway: 'razorpay', payload: { razorpay_payment_id: 'p', razorpay_signature: 's' } });
    expect(r.status).toBe('ok');
  });

  it('subscription get and billing preview/cancel/previewChange', async () => {
    const { f, c } = setup({ url: 'u' });
    await c.subscription.get('t1');
    expect(call(f, 0).url).toBe(`${P}subscription.get?tenant=t1`);
    await c.billing.previewCancel('t1');
    expect(call(f, 1).url).toBe(`${P}billing.preview_cancel?tenant=t1`);
    await c.billing.cancel('t1', { reason: 'r' });
    expect(call(f, 2).url).toBe(`${P}billing.cancel`);
    expect(bodyOf(f, 2)).toEqual({ tenant: 't1', reason: 'r' });
    expect(call(f, 2).init.headers.get('Idempotency-Key')).toBeTruthy();
    await c.billing.previewChange('t1', 'pro');
    expect(call(f, 3).url).toBe(`${P}billing.preview_change?tenant=t1&plan=pro`);
  });

  it('invoices list/get/pdf', async () => {
    const { f, c } = setup({ items: [], next_cursor: null });
    await c.invoices.list({ tenant: 't1' });
    expect(call(f, 0).url).toBe(`${P}invoices.list?tenant=t1`);
    await c.invoices.get('inv1');
    expect(call(f, 1).url).toBe(`${P}invoices.get?invoice=inv1`);
    const pf = vi.fn(async () => new Response('%PDF', { status: 200, headers: { 'Content-Type': 'application/pdf' } }));
    const blob = await mk(pf).invoices.pdf('inv1');
    expect(call(pf, 0).url).toBe(`${P}invoices.pdf?invoice=inv1`);
    expect(await blob.text()).toBe('%PDF');
    const nf = vi.fn(async () => err(404));
    expect(await mk(nf).invoices.pdf('x').catch((e) => e)).toBeInstanceOf(NotFoundError);
  });

  it('paymentMethods.portalLink', async () => {
    const { f, c } = setup({ url: 'u' });
    await c.paymentMethods.portalLink({ tenant: 't1', return_url: 'r' });
    expect(call(f, 0).url).toBe(`${P}payment_methods.portal_link`);
    expect(bodyOf(f, 0)).toEqual({ tenant: 't1', return_url: 'r' });
  });

  it('jobs get/list', async () => {
    const { f, c } = setup({ items: [], next_cursor: null });
    await c.jobs.list({ tenant: 't1' });
    expect(call(f, 0).url).toBe(`${P}jobs.list?tenant=t1`);
    await c.jobs.get('j1');
    expect(call(f, 1).url).toBe(`${P}jobs.get?job=j1`);
  });

  it('support list/get/create/reply', async () => {
    const { f, c } = setup({ items: [], next_cursor: null });
    await c.support.list();
    expect(call(f, 0).url).toBe(`${P}support.tickets.list`);
    await c.support.get('k1');
    expect(call(f, 1).url).toBe(`${P}support.tickets.get?ticket=k1`);
    await c.support.create({ subject: 's', body: 'b' });
    expect(call(f, 2).url).toBe(`${P}support.tickets.create`);
    expect(bodyOf(f, 2)).toEqual({ subject: 's', body: 'b' });
    await c.support.reply('k1', 'hi');
    expect(call(f, 3).url).toBe(`${P}support.tickets.reply`);
    expect(bodyOf(f, 3)).toEqual({ ticket: 'k1', body: 'hi' });
  });

  it('plans/templates lists unwrap items', async () => {
    const { f, c } = setup({ items: [{ name: 'p' }], next_cursor: null });
    expect(await c.plans.list({ currency: 'USD' })).toEqual([{ name: 'p' }]);
    expect(call(f, 0).url).toBe(`${P}plans.list?currency=USD`);
    expect(await c.templates.list()).toEqual([{ name: 'p' }]);
  });
});

describe('sse', () => {
  it('parser handles chunk splits, comments, multi-line data', () => {
    const out: any[] = [];
    const p = createSseParser((m) => out.push(m));
    p(': hi\nevent: job\nda'); p('ta: a\ndata: b\n\ndata: {"x":1}\r\n\r\n');
    expect(out).toEqual([{ event: 'job', data: 'a\nb', id: undefined }, { event: 'message', data: '{"x":1}', id: undefined }]);
  });
  it('streams contract events with auth headers and stops at terminal job', async () => {
    const f = vi.fn(async () => sse(
      `id: 1\ndata: ${JSON.stringify(ev('job.updated', { state: 'running' }, { job: 'j1' }))}\n\n` +
      `id: 2\ndata: ${JSON.stringify(ev('job.updated', { state: 'succeeded' }, { job: 'j1' }))}\n\n`));
    const evs: any[] = [];
    mk(f, { auth: { type: 'apiKey', key: 't' } }).subscribe({ jobId: 'j1' }, (e) => evs.push(e));
    await vi.waitFor(() => expect(evs.map((e) => e.data.state)).toEqual(['running', 'succeeded']));
    expect(call(f, 0).url).toBe(`${P}events.stream?job=j1`);
    expect(call(f, 0).init.headers.get('Authorization')).toBe('token t');
    expect(call(f, 0).init.headers.get('Accept')).toBe('text/event-stream');
    await new Promise((r) => setTimeout(r, 30));
    expect(f).toHaveBeenCalledTimes(1);
  });
  it('reconnects after stream failure', async () => {
    const f = vi.fn()
      .mockRejectedValueOnce(new TypeError('down'))
      .mockResolvedValueOnce(sse(`data: ${JSON.stringify(ev('job.updated', { state: 'failed' }, { job: 'j1' }))}\n\n`));
    const evs: any[] = [];
    const errs: any[] = [];
    mk(f).subscribe({ jobId: 'j1' }, (e) => evs.push(e), (e) => errs.push(e));
    await vi.waitFor(() => expect(evs).toHaveLength(1));
    expect(errs[0]).toBeInstanceOf(NetworkError);
    expect(f).toHaveBeenCalledTimes(2);
  });
  it('sends Last-Event-ID on reconnect after clean end', async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(sse(`id: 7\ndata: ${JSON.stringify(ev('job.updated', { state: 'running' }, { job: 'j1' }))}\n\n`))
      .mockResolvedValueOnce(sse(`id: 8\ndata: ${JSON.stringify(ev('job.updated', { state: 'succeeded' }, { job: 'j1' }))}\n\n`));
    const evs: any[] = [];
    mk(f).subscribe({ jobId: 'j1' }, (e) => evs.push(e));
    await vi.waitFor(() => expect(evs).toHaveLength(2));
    expect(call(f, 1).init.headers.get('Last-Event-ID')).toBe('7');
  });
  it('falls back to polling after repeated failures and stops at terminal', async () => {
    const seq = ['running', 'succeeded'];
    const f = vi.fn(async (url: string) => (url.includes('events.stream') ? err(500) : ok({ name: 'j1', state: seq.shift() ?? 'succeeded' })));
    const evs: any[] = [];
    mk(f, { pollIntervalMs: 5 }).subscribe({ jobId: 'j1' }, (e) => evs.push(e));
    await vi.waitFor(() => expect(evs.map((e) => e.data.state)).toEqual(['running', 'succeeded']));
    expect(evs[0].type).toBe('job.updated');
    const n = f.mock.calls.length;
    await new Promise((r) => setTimeout(r, 40));
    expect(f.mock.calls.length).toBe(n);
    expect(f.mock.calls.some((c: any) => c[0].includes('jobs.get?job=j1'))).toBe(true);
  });
  it('close() stops polling', async () => {
    const f = vi.fn(async (url: string) => (url.includes('events.stream') ? err(500) : ok({ name: 't', status: 'provisioning' })));
    const evs: any[] = [];
    const s = mk(f, { pollIntervalMs: 5 }).subscribe({ tenantId: 't' }, (e) => evs.push(e));
    await vi.waitFor(() => expect(evs.length).toBeGreaterThan(0));
    expect(evs[0].type).toBe('tenant.updated');
    s.close();
    await new Promise((r) => setTimeout(r, 20));
    const n = f.mock.calls.length;
    await new Promise((r) => setTimeout(r, 40));
    expect(f.mock.calls.length).toBe(n);
  });
});

describe('profile, tokens, team, restore', () => {
  const run = async (payload: unknown, fn: (c: any) => Promise<any>, extra: any = {}) => {
    const f = vi.fn(async () => ok(payload));
    const r = await fn(mk(f, extra));
    return { f, r };
  };
  it('me.update posts, unwraps and stores csrf', async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(ok({ email: 'a', csrf_token: 'C0' })) // bootstrap me
      .mockResolvedValueOnce(ok({ email: 'a', csrf_token: 'C9' }))
      .mockResolvedValueOnce(ok({}));
    const c = mk(f, { auth: { type: 'cookie' } });
    const r = await c.me.update({ full_name: 'N' } as any);
    expect(call(f, 1).url).toBe(`${P}me.update`);
    expect(call(f, 1).init.method).toBe('POST');
    expect(bodyOf(f, 1)).toEqual({ full_name: 'N' });
    expect(r.email).toBe('a');
    await c.tenants.openLoginLink('t1');
    expect(call(f, 2).init.headers.get('X-Frappe-CSRF-Token')).toBe('C9');
  });
  it('apiTokens list/create/revoke', async () => {
    let x = await run({ items: [{ name: 't' }], next_cursor: null }, (c) => c.apiTokens.list());
    expect(call(x.f, 0).url).toContain(`${P}api_tokens.list`);
    expect(call(x.f, 0).init.method).toBe('GET');
    expect(x.r).toEqual([{ name: 't' }]);
    x = await run({ token: 'sek' }, (c) => c.apiTokens.create({ label: 'l' }));
    expect(call(x.f, 0).url).toBe(`${P}api_tokens.create`);
    expect(call(x.f, 0).init.method).toBe('POST');
    expect(bodyOf(x.f, 0)).toEqual({ label: 'l' });
    expect(x.r).toEqual({ token: 'sek' });
    x = await run(null, (c) => c.apiTokens.revoke('tk'));
    expect(call(x.f, 0).url).toBe(`${P}api_tokens.revoke`);
    expect(bodyOf(x.f, 0)).toEqual({ token: 'tk' });
    expect(x.r).toBeUndefined();
  });
  it('team methods', async () => {
    let x = await run({ items: [{ name: 'm' }], next_cursor: null }, (c) => c.team.list('t1'));
    expect(call(x.f, 0).url).toContain(`${P}tenants.team.list`);
    expect(call(x.f, 0).url).toContain('tenant=t1');
    expect(call(x.f, 0).init.method).toBe('GET');
    expect(x.r).toEqual([{ name: 'm' }]);
    x = await run({ name: 'i' }, (c) => c.team.invite('t1', { email: 'e@x', role: 'admin' }));
    expect(call(x.f, 0).url).toBe(`${P}tenants.team.invite`);
    expect(bodyOf(x.f, 0)).toEqual({ tenant: 't1', email: 'e@x', role: 'admin' });
    expect(x.r).toEqual({ name: 'i' });
    x = await run(null, (c) => c.team.revokeInvite('t1', 'inv'));
    expect(call(x.f, 0).url).toBe(`${P}tenants.team.revoke_invite`);
    expect(bodyOf(x.f, 0)).toEqual({ tenant: 't1', invite: 'inv' });
    x = await run({ name: 'm' }, (c) => c.team.changeRole('t1', 'm', 'viewer'));
    expect(call(x.f, 0).url).toBe(`${P}tenants.team.change_role`);
    expect(bodyOf(x.f, 0)).toEqual({ tenant: 't1', member: 'm', role: 'viewer' });
    expect(x.r).toEqual({ name: 'm' });
    x = await run(null, (c) => c.team.remove('t1', 'm'));
    expect(call(x.f, 0).url).toBe(`${P}tenants.team.remove`);
    expect(bodyOf(x.f, 0)).toEqual({ tenant: 't1', member: 'm' });
    x = await run({ name: 'm' }, (c) => c.team.acceptInvite('tok'));
    expect(call(x.f, 0).url).toBe(`${P}team.accept_invite`);
    expect(bodyOf(x.f, 0)).toEqual({ token: 'tok' });
    expect(x.r).toEqual({ name: 'm' });
  });
  it('backups.restore returns approval', async () => {
    const x = await run({ approval: 'a1' }, (c) => c.backups.restore('t1', 'b1'));
    expect(call(x.f, 0).url).toBe(`${P}tenants.backups.restore`);
    expect(call(x.f, 0).init.method).toBe('POST');
    expect(bodyOf(x.f, 0)).toEqual({ tenant: 't1', backup: 'b1' });
    expect(x.r).toEqual({ approval: 'a1' });
  });
});

describe('v1.2 additions', () => {
  it('auth.requestEmailVerification posts to verify_email.request', async () => {
    const f = vi.fn(async () => ok(null, 202));
    await expect(mk(f).auth.requestEmailVerification()).resolves.toBeUndefined();
    expect(call(f, 0).url).toBe(`${P}verify_email.request`);
    expect(call(f, 0).init.method).toBe('POST');
  });
  it('auth.confirmEmail posts key to verify_email.confirm', async () => {
    const f = vi.fn(async () => new Response(null, { status: 204 }));
    await expect(mk(f).auth.confirmEmail('abc')).resolves.toBeUndefined();
    expect(call(f, 0).url).toBe(`${P}verify_email.confirm`);
    expect(call(f, 0).init.method).toBe('POST');
    expect(bodyOf(f, 0)).toEqual({ key: 'abc' });
  });
  it('auth.confirmEmail maps 422 to ValidationError', async () => {
    const f = vi.fn(async () => err(422));
    await expect(mk(f).auth.confirmEmail('bad')).rejects.toBeInstanceOf(ValidationError);
  });
  it('auth.requestEmailVerification maps 401 to AuthError', async () => {
    const f = vi.fn(async () => err(401));
    await expect(mk(f).auth.requestEmailVerification()).rejects.toBeInstanceOf(AuthError);
  });
  it('support.close posts ticket and returns ticket', async () => {
    const f = vi.fn(async () => ok({ name: 'TKT-1', status: 'closed' }));
    const t = await mk(f).support.close('TKT-1');
    expect(call(f, 0).url).toBe(`${P}support.tickets.close`);
    expect(call(f, 0).init.method).toBe('POST');
    expect(bodyOf(f, 0)).toEqual({ ticket: 'TKT-1' });
    expect(t.status).toBe('closed');
  });
  it('support.close maps 404 to NotFoundError', async () => {
    const f = vi.fn(async () => err(404));
    await expect(mk(f).support.close('nope')).rejects.toBeInstanceOf(NotFoundError);
  });
});

describe('v1.3 config + admin tenants', () => {
  it('config.get GETs config', async () => {
    const f = vi.fn(async () => ok({ public_signup: false, payment_banner: 'Test mode' }));
    const c = await mk(f).config.get();
    expect(call(f, 0).url).toBe(`${P}config`);
    expect(call(f, 0).init.method).toBe('GET');
    expect(c).toEqual({ public_signup: false, payment_banner: 'Test mode' });
  });
  it('admin.tenants.list passes filters and unwraps items; listPage keeps cursor', async () => {
    const f = vi.fn(async () => ok({ items: [{ name: 'a' }], next_cursor: 'n1' }));
    const items = await mk(f).admin.tenants.list({ status: 'active', owner_email: 'o@x.com', limit: 5 });
    const u = call(f, 0).url;
    expect(u).toContain(`${P}admin.tenants.list?`);
    expect(u).toContain('status=active'); expect(u).toContain('owner_email=o%40x.com'); expect(u).toContain('limit=5');
    expect(items).toEqual([{ name: 'a' }]);
    expect((await mk(f).admin.tenants.listPage()).next_cursor).toBe('n1');
  });
  it('admin.tenants.create posts body and returns TenantCreated', async () => {
    const f = vi.fn(async () => ok({ tenant: { name: 't' }, job: { name: 'j' }, checkout_required: false }, 202));
    const r = await mk(f).admin.tenants.create({ owner_email: 'o@x.com', plan: 'pro', subdomain: 'acme' });
    expect(call(f, 0).url).toBe(`${P}admin.tenants.create`);
    expect(call(f, 0).init.method).toBe('POST');
    expect(bodyOf(f, 0)).toEqual({ owner_email: 'o@x.com', plan: 'pro', subdomain: 'acme' });
    expect(r.job.name).toBe('j');
  });
  it('admin.tenants.invite posts body', async () => {
    const f = vi.fn(async () => ok({ name: 'TM-1', role: 'owner' }, 201));
    await mk(f).admin.tenants.invite({ tenant: 't', owner_email: 'o@x.com' });
    expect(call(f, 0).url).toBe(`${P}admin.tenants.invite`);
    expect(bodyOf(f, 0)).toEqual({ tenant: 't', owner_email: 'o@x.com' });
  });
  it('403 admin maps to ForbiddenError; signup_disabled code preserved', async () => {
    const f = vi.fn(async () => err(403, { code: 'signup_disabled', message: 'off' }));
    await expect(mk(f).admin.tenants.list()).rejects.toBeInstanceOf(ForbiddenError);
    await expect(mk(f).auth.signup({ email: 'a@b.co', password: 'longpassword1' } as any)).rejects.toMatchObject({ code: 'signup_disabled', status: 403 });
  });
  it('503 capacity_exhausted maps to ServerError with code', async () => {
    const f = vi.fn(async () => err(503, { code: 'capacity_exhausted', message: 'full' }));
    await expect(mk(f).admin.tenants.create({ owner_email: 'o@x.com', plan: 'pro' })).rejects.toMatchObject({ code: 'capacity_exhausted', status: 503 });
  });
});

describe('admin.betaAccess', () => {
  it('list is a GET to the contract path', async () => {
    const f = vi.fn(async () => ok({ signup_mode: 'invite', patterns: [] }));
    const r = await mk(f, { auth: { type: 'apiKey', key: 't' } }).admin.betaAccess.list();
    expect(call(f, 0).url).toBe(`${P}admin.beta_access.list`);
    expect(call(f, 0).init.method).toBe('GET');
    expect(r.patterns).toEqual([]);
  });
  it('add posts entries, send_invite and note', async () => {
    const res = { added: ['a@b.co'], already_listed: [], invites_sent: ['a@b.co'], invites_failed: [], skipped_domains: [] };
    const f = vi.fn(async () => ok(res));
    const r = await mk(f, { auth: { type: 'apiKey', key: 't' } }).admin.betaAccess.add({ entries: ['a@b.co'], send_invite: true, note: 'n' });
    expect(call(f, 0).url).toBe(`${P}admin.beta_access.add`);
    expect(call(f, 0).init.method).toBe('POST');
    expect(bodyOf(f, 0)).toEqual({ entries: ['a@b.co'], send_invite: true, note: 'n' });
    expect(r).toEqual(res);
  });
  it('remove and resend post their single field', async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(ok({ removed: '@b.co' }))
      .mockResolvedValueOnce(ok({ email: 'a@b.co', sent: true }));
    const c = mk(f, { auth: { type: 'apiKey', key: 't' } });
    expect((await c.admin.betaAccess.remove('@b.co')).removed).toBe('@b.co');
    expect(call(f, 0).url).toBe(`${P}admin.beta_access.remove`);
    expect(bodyOf(f, 0)).toEqual({ pattern: '@b.co' });
    expect((await c.admin.betaAccess.resend('a@b.co')).sent).toBe(true);
    expect(call(f, 1).url).toBe(`${P}admin.beta_access.resend`);
    expect(bodyOf(f, 1)).toEqual({ email: 'a@b.co' });
  });
  it('maps a 403 to ForbiddenError', async () => {
    const f = vi.fn(async () => err(403, { code: 'forbidden', message: 'System Manager role required' }));
    await expect(mk(f, { auth: { type: 'apiKey', key: 't' } }).admin.betaAccess.list()).rejects.toBeInstanceOf(ForbiddenError);
  });
});

describe('contract fields', () => {
  it('Me.email_verified and Job.started_at / step aliases are typed', async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(ok({ email: 'a', email_verified: true }))
      .mockResolvedValueOnce(ok({ name: 'j', started_at: '2026-01-01T00:00:00Z', steps: [{ name: 's', key: 's', state: 'running', status: 'running' }] }));
    const c = mk(f, { auth: { type: 'apiKey', key: 't' } });
    const me = await c.me.get();
    const verified: boolean | undefined = me.email_verified;
    expect(verified).toBe(true);
    const job = await c.jobs.get('j');
    const startedAt: string | null | undefined = job.started_at;
    expect(startedAt).toBe('2026-01-01T00:00:00Z');
    const step: { key?: string; status?: string } | undefined = job.steps?.[0];
    expect(step?.key).toBe('s');
  });
});
