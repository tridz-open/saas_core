import { describe, it, expect, vi } from 'vitest';
import { createApp, defineComponent, effectScope, h, nextTick, ref } from 'vue';
import type { SaasClient } from '@frappe-saaskit/client';
import { SAAS_CLIENT_KEY, useTenants, useTenant, useCreateTenant, useJob, useSaasClient, useRequestDeleteTenant, useDeleteTenant, useSubscription, useCheckout, useLogin, useBackupDownloadLink, useReplyToTicket, useTenantLive, useTemplates, useUpdateProfile, useApiTokens, useCreateApiToken, useRevokeApiToken, useTeamMembers, useInviteMember, useRevokeInvite, useChangeMemberRole, useRemoveMember, useAcceptInvite, useRestoreBackup, useRequestEmailVerification, useConfirmEmail, useCloseTicket, usePublicConfig, useAdminTenants, useAdminCreateTenant, useAdminInviteTenant } from './index';

const flush = async () => { await Promise.resolve(); await nextTick(); await new Promise((r) => setTimeout(r, 0)); };
function fake(over: Record<string, unknown> = {}) {
  const subs: { cb: (e: any) => void; close: ReturnType<typeof vi.fn> }[] = [];
  const c = {
    tenants: { list: vi.fn(async () => [{ name: 't1' }]), get: vi.fn(async (id: string) => ({ name: id })), create: vi.fn(async (i: any) => ({ tenant: { name: 'n', ...i }, job: { name: 'j' } })) },
    jobs: { get: vi.fn(async (id: string) => ({ name: id, state: 'running' })) },
    subscribe: vi.fn((_t: any, cb: any) => { const s = { cb, close: vi.fn() }; subs.push(s); return s; }),
    ...over,
  } as unknown as SaasClient;
  return { c, subs };
}
function inScope<T>(c: SaasClient, fn: () => T) {
  const app = createApp(defineComponent({ render: () => h('div') })); app.provide(SAAS_CLIENT_KEY, c);
  const scope = effectScope(); let out!: T;
  app.runWithContext(() => { scope.run(() => { out = fn(); }); });
  return { out, scope };
}

describe('vue composables', () => {
  it('throws without provider', () => { expect(() => useSaasClient()).toThrow(/provideSaas/); });
  it('useSaasClient resolves app-provided client', () => {
    const { c } = fake(); const { out } = inScope(c, () => useSaasClient()); expect(out).toBe(c);
  });
  it('query returns data/loading and reload alias', async () => {
    const { c } = fake(); const { out } = inScope(c, () => useTenants());
    expect(out.loading.value).toBe(true); await flush();
    expect(out.data.value).toEqual([{ name: 't1' }]); expect(out.loading.value).toBe(false);
    await out.reload(); expect((c.tenants.list as any).mock.calls.length).toBe(2);
  });
  it('query captures errors', async () => {
    const { c } = fake({ tenants: { list: async () => { throw new Error('boom'); } } });
    const { out } = inScope(c, () => useTenants()); await flush();
    expect(out.error.value?.message).toBe('boom'); expect(out.data.value).toBeUndefined();
  });
  it('useTenant refetches on ref change and skips undefined', async () => {
    const { c } = fake(); const id = ref<string | undefined>(undefined);
    const { out } = inScope(c, () => useTenant(id)); await flush();
    expect((c.tenants.get as any)).not.toHaveBeenCalled();
    id.value = 'a'; await flush(); expect(out.data.value).toEqual({ name: 'a' });
  });
  it('mutation', async () => {
    const { c } = fake(); const { out } = inScope(c, () => useCreateTenant());
    const r = await out.mutate({ slug: 'x', plan: 'p' } as any);
    expect(r.job.name).toBe('j'); expect(out.data.value?.tenant.name).toBe('n'); expect(out.loading.value).toBe(false);
  });
  it('mutation error rethrows and sets error', async () => {
    const { c } = fake({ tenants: { create: async () => { throw new Error('no'); } } });
    const { out } = inScope(c, () => useCreateTenant());
    await expect(out.mutate({ slug: 'x', plan: 'p' } as any)).rejects.toThrow('no');
    expect(out.error.value?.message).toBe('no');
  });
  it('useJob refetches on event and stops at terminal state', async () => {
    const get = vi.fn().mockResolvedValueOnce({ name: 'j1', state: 'running' }).mockResolvedValueOnce({ name: 'j1', state: 'succeeded' });
    const { c, subs } = fake({ jobs: { get } }); const { out, scope } = inScope(c, () => useJob('j1')); await flush();
    expect(out.data.value?.state).toBe('running'); expect(subs).toHaveLength(1);
    subs[0].cb({ seq: 1, type: 'job.updated', job: 'j1' }); await flush();
    expect(out.data.value?.state).toBe('succeeded'); expect(subs[0].close).toHaveBeenCalled();
    scope.stop();
  });
  it('useTenantLive subscribes by tenantId and closes on dispose', async () => {
    const { c, subs } = fake(); const { out, scope } = inScope(c, () => useTenantLive('t1')); await flush();
    expect((c.subscribe as any).mock.calls[0][0]).toEqual({ tenantId: 't1' }); expect(out.data.value).toEqual({ name: 't1' });
    scope.stop(); expect(subs[0].close).toHaveBeenCalled();
  });
  it('requestDelete + deprecated alias', async () => {
    const requestDelete = vi.fn(async () => ({ name: 'j' }));
    const { c } = fake({ tenants: { requestDelete } });
    const { out } = inScope(c, () => useRequestDeleteTenant());
    await out.mutate('t1', { confirm_slug: 's', reason: 'r' });
    expect(requestDelete).toHaveBeenCalledWith('t1', { confirm_slug: 's', reason: 'r' });
    expect(useDeleteTenant).toBe(useRequestDeleteTenant);
  });
  it('useSubscription takes tenant id; checkout uses createSession', async () => {
    const get = vi.fn(async (t: string) => ({ tenant: t })); const createSession = vi.fn(async () => ({ url: 'u' }));
    const { c } = fake({ subscription: { get }, checkout: { createSession }, templates: { list: async () => [{ name: 'tp' }] } });
    const sub = inScope(c, () => useSubscription('t9')).out; await flush();
    expect(get).toHaveBeenCalledWith('t9'); expect(sub.data.value).toEqual({ tenant: 't9' });
    const co = inScope(c, () => useCheckout()).out; await co.mutate({ plan: 'p' } as any); expect(createSession).toHaveBeenCalled();
    const tp = inScope(c, () => useTemplates()).out; await flush(); expect(tp.data.value).toEqual([{ name: 'tp' }]);
  });
  it('auth, backup link and ticket reply mutations delegate', async () => {
    const login = vi.fn(async () => ({ user: 'u' })); const downloadLink = vi.fn(async () => ({ url: 'x' })); const reply = vi.fn(async () => ({ name: 'tk' }));
    const { c } = fake({ auth: { login }, backups: { downloadLink }, support: { reply } });
    await inScope(c, () => useLogin()).out.mutate({ email: 'e', password: 'p' } as any); expect(login).toHaveBeenCalled();
    await inScope(c, () => useBackupDownloadLink()).out.mutate('t', 'b', 'database'); expect(downloadLink).toHaveBeenCalledWith('t', 'b', 'database');
    await inScope(c, () => useReplyToTicket()).out.mutate('tk', 'hi'); expect(reply).toHaveBeenCalledWith('tk', 'hi');
  });
  it('profile, token, team and restore composables delegate', async () => {
    const f = () => vi.fn(async (..._a: any[]) => ({ ok: 1 }));
    const m = { me: { update: f() }, apiTokens: { list: vi.fn(async () => [{ name: 'k' }]), create: f(), revoke: f() },
      team: { list: vi.fn(async (t: string) => [{ t }]), invite: f(), revokeInvite: f(), changeRole: f(), remove: f(), acceptInvite: f() }, backups: { restore: f() } };
    const { c } = fake(m);
    await inScope(c, () => useUpdateProfile()).out.mutate({ full_name: 'A' }); expect(m.me.update).toHaveBeenCalledWith({ full_name: 'A' });
    const tk = inScope(c, () => useApiTokens()).out; await flush(); expect(tk.data.value).toEqual([{ name: 'k' }]);
    await inScope(c, () => useCreateApiToken()).out.mutate({ label: 'l' } as any); expect(m.apiTokens.create).toHaveBeenCalledWith({ label: 'l' });
    await inScope(c, () => useRevokeApiToken()).out.mutate('k'); expect(m.apiTokens.revoke).toHaveBeenCalledWith('k');
    const tm = inScope(c, () => useTeamMembers('t1')).out; await flush(); expect(tm.data.value).toEqual([{ t: 't1' }]);
    await inScope(c, () => useInviteMember()).out.mutate('t1', { email: 'e', role: 'admin' } as any); expect(m.team.invite).toHaveBeenCalledWith('t1', { email: 'e', role: 'admin' });
    await inScope(c, () => useRevokeInvite()).out.mutate('t1', 'i'); expect(m.team.revokeInvite).toHaveBeenCalledWith('t1', 'i');
    await inScope(c, () => useChangeMemberRole()).out.mutate('t1', 'm', 'admin' as any); expect(m.team.changeRole).toHaveBeenCalledWith('t1', 'm', 'admin');
    await inScope(c, () => useRemoveMember()).out.mutate('t1', 'm'); expect(m.team.remove).toHaveBeenCalledWith('t1', 'm');
    await inScope(c, () => useAcceptInvite()).out.mutate('tok'); expect(m.team.acceptInvite).toHaveBeenCalledWith('tok');
    await inScope(c, () => useRestoreBackup()).out.mutate('t1', 'b'); expect(m.backups.restore).toHaveBeenCalledWith('t1', 'b');
  });
});

describe('v1.2 composables', () => {
  it('email verification and close ticket delegate', async () => {
    const m = { auth: { requestEmailVerification: vi.fn(async () => {}), confirmEmail: vi.fn(async (_k: string) => {}) }, support: { close: vi.fn(async (t: string) => ({ name: t, status: 'closed' })) } };
    const { c } = fake(m);
    await inScope(c, () => useRequestEmailVerification()).out.mutate(); expect(m.auth.requestEmailVerification).toHaveBeenCalledTimes(1);
    await inScope(c, () => useConfirmEmail()).out.mutate('K'); expect(m.auth.confirmEmail).toHaveBeenCalledWith('K');
    const close = inScope(c, () => useCloseTicket()).out;
    const r = await close.mutate('tk'); expect(m.support.close).toHaveBeenCalledWith('tk');
    expect(r.status).toBe('closed'); expect(close.data.value?.status).toBe('closed');
  });
  it('useConfirmEmail surfaces error', async () => {
    const { c } = fake({ auth: { confirmEmail: async () => { throw new Error('bad'); } } });
    const { out } = inScope(c, () => useConfirmEmail());
    await expect(out.mutate('K')).rejects.toThrow('bad'); expect(out.error.value?.message).toBe('bad');
  });
  it('usePublicConfig and useAdminTenants load data', async () => {
    const { c } = fake({ config: { get: vi.fn(async () => ({ public_signup: true, payment_banner: null })) }, admin: { tenants: { list: vi.fn(async () => [{ name: 'a' }]) } } });
    const a = inScope(c, () => usePublicConfig()); const b = inScope(c, () => useAdminTenants({ status: 'active' })); await flush();
    expect(a.out.data.value).toEqual({ public_signup: true, payment_banner: null });
    expect(b.out.data.value).toEqual([{ name: 'a' }]);
    expect((c as any).admin.tenants.list).toHaveBeenCalledWith({ status: 'active' });
  });
  it('useAdminCreateTenant and useAdminInviteTenant call admin endpoints', async () => {
    const create = vi.fn(async () => ({ tenant: { name: 't' }, job: { name: 'j' } })); const invite = vi.fn(async () => ({ name: 'TM-1' }));
    const { c } = fake({ admin: { tenants: { create, invite } } });
    const a = inScope(c, () => useAdminCreateTenant()); const b = inScope(c, () => useAdminInviteTenant());
    await a.out.mutate({ owner_email: 'o@x.com', plan: 'pro' }); await b.out.mutate({ tenant: 't', owner_email: 'o@x.com' });
    expect(create).toHaveBeenCalledWith({ owner_email: 'o@x.com', plan: 'pro' });
    expect(invite).toHaveBeenCalledWith({ tenant: 't', owner_email: 'o@x.com' });
  });
});
