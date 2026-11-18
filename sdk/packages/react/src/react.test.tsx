// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, renderHook, waitFor, act, cleanup } from '@testing-library/react';
import { QueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import type { SaasClient, SaasEvent, Job, Tenant } from '@frappe-saaskit/client';
import {
  SaasProvider, useSaasClient, usePlans, useCreateTenant, useTenants, useJob, useTenantLive, saasKeys,
  useRequestDeleteTenant, useDeleteTenant, useChangePlan, useCheckout, useSubscription, useLogin, useLogout, useMe,
  useTemplates, useInvoicePdf, useJobs, useReplyToTicket, useSupportTicket, useBackupDownloadLink, useOpenLoginLink,
  useUpdateProfile, useApiTokens, useCreateApiToken, useRevokeApiToken, useTeamMembers, useInviteMember, useRevokeInvite,
  useChangeMemberRole, useRemoveMember, useAcceptInvite, useRestoreBackup,
  useSuspendStatus, useInvoice, usePreviewCancel, usePaymentMethodsPortalLink, useRequestPasswordReset,
  useRequestEmailVerification, useConfirmEmail, useCloseTicket,
  usePublicConfig, useAdminTenants, useAdminCreateTenant, useAdminInviteTenant,
} from './index';

afterEach(cleanup);

const job = (state: Job['state'], extra: Partial<Job> = {}): Job => ({ name: 'j1', tenant: 't1', kind: 'provision', state, ...extra });
const tenant = (status: Tenant['status'] = 'pending'): Tenant => ({ name: 't1', slug: 'a', status, plan: 'pro' } as Tenant);
const qcNew = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

function fake(over: Record<string, unknown> = {}) {
  let emit: (e: SaasEvent) => void = () => {};
  const close = vi.fn();
  const state = { job: job('running'), tenant: tenant() };
  const client = {
    plans: { list: vi.fn(async () => [{ code: 'pro', title: 'Pro', prices: [], limits: {} }]) },
    templates: { list: vi.fn(async () => [{ name: 'tpl' }]) },
    auth: { login: vi.fn(async () => ({ email: 'a@b.c' })), logout: vi.fn(async () => {}), requestPasswordReset: vi.fn(async () => {}) },
    me: { get: vi.fn(async () => ({ email: 'x' })) },
    tenants: {
      list: vi.fn(async () => [tenant()]), get: vi.fn(async () => state.tenant),
      create: vi.fn(async () => ({ tenant: tenant(), job: job('pending') })),
      requestDelete: vi.fn(async () => job('pending', { kind: 'terminate' })),
      changePlan: vi.fn(async () => job('pending', { name: 'j2', kind: 'change_plan' })),
      suspendStatus: vi.fn(async () => ({ suspended: false })), openLoginLink: vi.fn(async () => ({ url: 'u' })),
    },
    backups: { downloadLink: vi.fn(async () => ({ url: 'd' })) },
    checkout: { createSession: vi.fn(async () => ({ url: 'c' })) },
    subscription: { get: vi.fn(async () => ({ tenant: 't1', plan: 'pro', status: 'active' })) },
    billing: { previewCancel: vi.fn(async () => ({ can_cancel: true })), cancel: vi.fn(async () => ({ status: 'canceling' })), previewChange: vi.fn(async () => ({ direction: 'upgrade' })) },
    invoices: { get: vi.fn(async () => ({ name: 'i1' })), pdf: vi.fn(async () => new Blob(['x'])) },
    paymentMethods: { portalLink: vi.fn(async () => ({ url: 'pm' })) },
    jobs: { get: vi.fn(async () => state.job), list: vi.fn(async () => [state.job]) },
    support: { get: vi.fn(async () => ({ name: 'tk' })), reply: vi.fn(async () => ({ name: 'tk', subject: 'r' })) },
    subscribe: vi.fn((_t, on) => { emit = on; return { close }; }),
    ...over,
  } as unknown as SaasClient;
  return { client, close, state, emit: (e: SaasEvent) => emit(e) };
}
const ev = (type: SaasEvent['type']): SaasEvent => ({ seq: 1, type, at: 'x', data: {} }) as SaasEvent;
const wrap = (client: SaasClient, qc?: QueryClient) => ({ children }: { children: ReactNode }) => <SaasProvider client={client} queryClient={qc}>{children}</SaasProvider>;

describe('SaasProvider', () => {
  it('supplies client and throws outside provider', () => {
    const { client } = fake();
    const { result } = renderHook(() => useSaasClient(), { wrapper: wrap(client) });
    expect(result.current).toBe(client);
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => renderHook(() => useSaasClient())).toThrow(/SaasProvider/);
    spy.mockRestore();
  });
  it('renders children', () => {
    const { client } = fake();
    const r = render(<SaasProvider client={client}><p>hi</p></SaasProvider>);
    expect(r.getByText('hi')).toBeTruthy();
  });
});

describe('queries', () => {
  it('usePlans / useTemplates / useSubscription / useJobs / useSupportTicket / useInvoice / useSuspendStatus', async () => {
    const { client } = fake();
    const { result } = renderHook(() => ({
      plans: usePlans(), tpl: useTemplates(), sub: useSubscription('t1'), jobs: useJobs('t1'),
      tk: useSupportTicket('tk'), inv: useInvoice('i1'), sus: useSuspendStatus('t1'),
    }), { wrapper: wrap(client) });
    await waitFor(() => {
      expect(result.current.plans.data?.[0]?.code).toBe('pro');
      expect(result.current.tpl.data).toHaveLength(1);
      expect(result.current.sub.data?.plan).toBe('pro');
      expect(result.current.jobs.data).toHaveLength(1);
      expect(result.current.tk.data?.name).toBe('tk');
      expect(result.current.inv.data?.name).toBe('i1');
      expect(result.current.sus.data).toBeTruthy();
    });
    expect(client.subscription.get).toHaveBeenCalledWith('t1');
    expect(client.jobs.list).toHaveBeenCalledWith({ tenant: 't1' });
  });
});

describe('mutations', () => {
  it('useCreateTenant invalidates tenants and seeds job + tenant', async () => {
    const { client } = fake();
    const qc = qcNew();
    const { result } = renderHook(() => ({ list: useTenants(), create: useCreateTenant() }), { wrapper: wrap(client, qc) });
    await waitFor(() => expect(result.current.list.data).toHaveLength(1));
    await act(async () => { await result.current.create.mutateAsync({ slug: 'a', plan: 'pro' }); });
    await waitFor(() => expect(client.tenants.list).toHaveBeenCalledTimes(2));
    expect(qc.getQueryData<Job>(saasKeys.job('j1'))?.state).toBe('pending');
    expect(qc.getQueryData<Tenant>(saasKeys.tenant('t1'))?.slug).toBe('a');
  });
  it('useRequestDeleteTenant seeds job; useDeleteTenant is alias', async () => {
    const { client } = fake();
    const qc = qcNew();
    expect(useDeleteTenant).toBe(useRequestDeleteTenant);
    const { result } = renderHook(() => useRequestDeleteTenant(), { wrapper: wrap(client, qc) });
    await act(async () => { await result.current.mutateAsync({ id: 't1', confirm_slug: 'a', reason: 'r' }); });
    expect(client.tenants.requestDelete).toHaveBeenCalledWith('t1', { confirm_slug: 'a', reason: 'r' });
    expect(qc.getQueryData<Job>(saasKeys.job('j1'))?.kind).toBe('terminate');
  });
  it('useChangePlan seeds returned job', async () => {
    const { client } = fake();
    const qc = qcNew();
    const { result } = renderHook(() => useChangePlan(), { wrapper: wrap(client, qc) });
    await act(async () => { await result.current.mutateAsync({ id: 't1', plan: 'biz' }); });
    expect(client.tenants.changePlan).toHaveBeenCalledWith('t1', 'biz');
    expect(qc.getQueryData<Job>(saasKeys.job('j2'))).toBeTruthy();
  });
  it('useCheckout calls checkout.createSession', async () => {
    const { client } = fake();
    const { result } = renderHook(() => useCheckout(), { wrapper: wrap(client) });
    let r; await act(async () => { r = await result.current.mutateAsync({ tenant: 't1', plan: 'pro' } as never); });
    expect(r).toEqual({ url: 'c' });
  });
  it('link/pdf/reply/reset mutations delegate', async () => {
    const { client } = fake();
    const { result } = renderHook(() => ({
      login: useOpenLoginLink(), dl: useBackupDownloadLink(), pdf: useInvoicePdf(), reply: useReplyToTicket(),
      sp: usePreviewCancel(), pm: usePaymentMethodsPortalLink(), rp: useRequestPasswordReset(),
    }), { wrapper: wrap(client) });
    await act(async () => {
      await result.current.login.mutateAsync('t1');
      await result.current.dl.mutateAsync({ tenantId: 't1', backup: 'b', part: 'database' });
      expect(await result.current.pdf.mutateAsync('i1')).toBeInstanceOf(Blob);
      await result.current.reply.mutateAsync({ ticket: 'tk', body: 'hi' });
      await result.current.sp.mutateAsync('t1');
      await result.current.pm.mutateAsync();
      await result.current.rp.mutateAsync('a@b.c');
    });
    expect(client.backups.downloadLink).toHaveBeenCalledWith('t1', 'b', 'database');
    expect(client.support.reply).toHaveBeenCalledWith('tk', 'hi');
    expect(client.billing.previewCancel).toHaveBeenCalledWith('t1');
    expect(client.auth.requestPasswordReset).toHaveBeenCalledWith('a@b.c');
  });
});

describe('auth', () => {
  it('useLogin clears saas queries and sets me; useLogout clears', async () => {
    const { client } = fake();
    const qc = qcNew();
    qc.setQueryData(saasKeys.tenants(), [tenant()]);
    const { result } = renderHook(() => ({ login: useLogin(), logout: useLogout() }), { wrapper: wrap(client, qc) });
    await act(async () => { await result.current.login.mutateAsync({ email: 'a@b.c', password: 'x' }); });
    expect(qc.getQueryData(saasKeys.tenants())).toBeUndefined();
    expect(qc.getQueryData<{ email: string }>(saasKeys.me())?.email).toBe('a@b.c');
    await act(async () => { await result.current.logout.mutateAsync(); });
    expect(qc.getQueryData(saasKeys.me())).toBeUndefined();
  });
  it('useMe fetches', async () => {
    const { client } = fake();
    const { result } = renderHook(() => useMe(), { wrapper: wrap(client) });
    await waitFor(() => expect(result.current.data).toBeTruthy());
  });
});

describe('useJob', () => {
  it('refetches on events, stops at terminal state, cleans up', async () => {
    const f = fake();
    const qc = qcNew();
    const { result, unmount } = renderHook(() => useJob('j1'), { wrapper: wrap(f.client, qc) });
    await waitFor(() => expect(result.current.data?.state).toBe('running'));
    expect(f.client.subscribe).toHaveBeenCalledWith({ jobId: 'j1' }, expect.any(Function));
    f.state.job = job('running', { progress: 50 });
    act(() => f.emit(ev('job.updated')));
    await waitFor(() => expect(result.current.data?.progress).toBe(50));
    f.state.job = job('succeeded');
    act(() => f.emit(ev('job.updated')));
    await waitFor(() => expect(result.current.data?.state).toBe('succeeded'));
    await waitFor(() => expect(f.close).toHaveBeenCalled());
    unmount();
  });
  it('closes subscription on unmount', async () => {
    const f = fake();
    const { result, unmount } = renderHook(() => useJob('j1'), { wrapper: wrap(f.client) });
    await waitFor(() => expect(result.current.data).toBeTruthy());
    unmount();
    expect(f.close).toHaveBeenCalled();
  });
});

describe('useTenantLive', () => {
  it('refetches on tenant.updated and closes when stable', async () => {
    const f = fake();
    const { result } = renderHook(() => useTenantLive('t1'), { wrapper: wrap(f.client) });
    await waitFor(() => expect(result.current.data?.status).toBe('pending'));
    expect(f.client.subscribe).toHaveBeenCalledWith({ tenantId: 't1' }, expect.any(Function));
    f.state.tenant = tenant('active');
    act(() => f.emit(ev('tenant.updated')));
    await waitFor(() => expect(result.current.data?.status).toBe('active'));
    await waitFor(() => expect(f.close).toHaveBeenCalled());
  });
});

describe('v0.3 hooks', () => {
  it('useUpdateProfile sets me', async () => {
    const f = fake({ me: { get: vi.fn(), update: vi.fn(async () => ({ email: 'n' })) } }); const qc = qcNew();
    const { result } = renderHook(() => useUpdateProfile(), { wrapper: wrap(f.client, qc) });
    await act(async () => { await result.current.mutateAsync({ full_name: 'N' }); });
    expect(qc.getQueryData(saasKeys.me())).toEqual({ email: 'n' });
  });
  it('api tokens: list, create (secret in result) and revoke invalidate list', async () => {
    const apiTokens = { list: vi.fn(async () => [{ name: 'k1' }]), create: vi.fn(async () => ({ name: 'k2', token: 'sek' })), revoke: vi.fn(async () => {}) };
    const f = fake({ apiTokens }); const qc = qcNew();
    const w = wrap(f.client, qc);
    const list = renderHook(() => useApiTokens(), { wrapper: w });
    await waitFor(() => expect(list.result.current.data).toHaveLength(1));
    const create = renderHook(() => useCreateApiToken(), { wrapper: w });
    let r: unknown; await act(async () => { r = await create.result.current.mutateAsync({ label: 'x' }); });
    expect(r).toMatchObject({ token: 'sek' });
    await waitFor(() => expect(apiTokens.list).toHaveBeenCalledTimes(2));
    const rev = renderHook(() => useRevokeApiToken(), { wrapper: w });
    await act(async () => { await rev.result.current.mutateAsync('k1'); });
    expect(apiTokens.revoke).toHaveBeenCalledWith('k1');
    await waitFor(() => expect(apiTokens.list).toHaveBeenCalledTimes(3));
  });
  it('team mutations call client and invalidate team list', async () => {
    const team = {
      list: vi.fn(async () => []), invite: vi.fn(async () => ({})), revokeInvite: vi.fn(async () => {}),
      changeRole: vi.fn(async () => ({})), remove: vi.fn(async () => {}), acceptInvite: vi.fn(async () => ({})),
    };
    const f = fake({ team }); const w = wrap(f.client, qcNew());
    renderHook(() => useTeamMembers('t1'), { wrapper: w });
    const q = renderHook(() => useTeamMembers('t1'), { wrapper: w });
    await waitFor(() => expect(q.result.current.isSuccess).toBe(true));
    let n = team.list.mock.calls.length;
    const run = async <T,>(hook: () => T, call: (h: T) => Promise<unknown>) => {
      const h = renderHook(hook, { wrapper: w }); await act(async () => { await call(h.result.current); });
      await waitFor(() => expect(team.list.mock.calls.length).toBeGreaterThan(n)); n = team.list.mock.calls.length;
    };
    await run(useInviteMember, (m) => m.mutateAsync({ tenantId: 't1', email: 'a@b.c', role: 'admin' } as never));
    expect(team.invite).toHaveBeenCalledWith('t1', { email: 'a@b.c', role: 'admin' });
    await run(useRevokeInvite, (m) => m.mutateAsync({ tenantId: 't1', invite: 'i1' }));
    expect(team.revokeInvite).toHaveBeenCalledWith('t1', 'i1');
    await run(useChangeMemberRole, (m) => m.mutateAsync({ tenantId: 't1', member: 'm1', role: 'admin' } as never));
    expect(team.changeRole).toHaveBeenCalledWith('t1', 'm1', 'admin');
    await run(useRemoveMember, (m) => m.mutateAsync({ tenantId: 't1', member: 'm1' }));
    expect(team.remove).toHaveBeenCalledWith('t1', 'm1');
    const acc = renderHook(() => useAcceptInvite(), { wrapper: w });
    await act(async () => { await acc.result.current.mutateAsync('tok'); });
    expect(team.acceptInvite).toHaveBeenCalledWith('tok');
  });
  it('useRestoreBackup returns approval and does not seed a job', async () => {
    const backups = { restore: vi.fn(async () => ({ name: 'ap1', status: 'pending' })) };
    const f = fake({ backups }); const qc = qcNew();
    const { result } = renderHook(() => useRestoreBackup(), { wrapper: wrap(f.client, qc) });
    let r: unknown; await act(async () => { r = await result.current.mutateAsync({ tenantId: 't1', backup: 'b1' }); });
    expect(r).toMatchObject({ status: 'pending' });
    expect(backups.restore).toHaveBeenCalledWith('t1', 'b1');
    expect(qc.getQueryCache().findAll({ queryKey: ['saas', 'job'] })).toHaveLength(0);
  });
});

describe('v1.2 hooks', () => {
  it('useRequestEmailVerification calls client', async () => {
    const requestEmailVerification = vi.fn(async () => {});
    const { client } = fake({ auth: { requestEmailVerification } });
    const { result } = renderHook(() => useRequestEmailVerification(), { wrapper: wrap(client) });
    await act(async () => { await result.current.mutateAsync(); });
    expect(requestEmailVerification).toHaveBeenCalledTimes(1);
  });
  it('useConfirmEmail passes key and invalidates me', async () => {
    const confirmEmail = vi.fn(async () => {});
    const { client } = fake({ auth: { confirmEmail } });
    const qc = qcNew(); const spy = vi.spyOn(qc, 'invalidateQueries');
    const { result } = renderHook(() => useConfirmEmail(), { wrapper: wrap(client, qc) });
    await act(async () => { await result.current.mutateAsync('K'); });
    expect(confirmEmail).toHaveBeenCalledWith('K');
    expect(spy).toHaveBeenCalledWith({ queryKey: saasKeys.me() });
  });
  it('useConfirmEmail does not invalidate on error', async () => {
    const confirmEmail = vi.fn(async () => { throw new Error('bad'); });
    const { client } = fake({ auth: { confirmEmail } });
    const qc = qcNew(); const spy = vi.spyOn(qc, 'invalidateQueries');
    const { result } = renderHook(() => useConfirmEmail(), { wrapper: wrap(client, qc) });
    await act(async () => { await expect(result.current.mutateAsync('K')).rejects.toThrow('bad'); });
    expect(spy).not.toHaveBeenCalled();
  });
  it('useCloseTicket caches ticket and invalidates support list', async () => {
    const closed = { name: 'tk', status: 'closed' };
    const close = vi.fn(async () => closed);
    const { client } = fake({ support: { close } });
    const qc = qcNew(); const spy = vi.spyOn(qc, 'invalidateQueries');
    const { result } = renderHook(() => useCloseTicket(), { wrapper: wrap(client, qc) });
    await act(async () => { await result.current.mutateAsync('tk'); });
    expect(close).toHaveBeenCalledWith('tk');
    expect(qc.getQueryData(saasKeys.ticket('tk'))).toEqual(closed);
    expect(spy).toHaveBeenCalledWith({ queryKey: saasKeys.support() });
  });
  it('usePublicConfig fetches config', async () => {
    const get = vi.fn(async () => ({ public_signup: false, payment_banner: 'Test' }));
    const { client } = fake({ config: { get } });
    const { result } = renderHook(() => usePublicConfig(), { wrapper: wrap(client, qcNew()) });
    await waitFor(() => expect(result.current.data).toEqual({ public_signup: false, payment_banner: 'Test' }));
  });
  it('useAdminTenants lists with filters', async () => {
    const list = vi.fn(async () => [{ name: 'a' }]);
    const { client } = fake({ admin: { tenants: { list } } });
    const { result } = renderHook(() => useAdminTenants({ status: 'active' }), { wrapper: wrap(client, qcNew()) });
    await waitFor(() => expect(result.current.data).toEqual([{ name: 'a' }]));
    expect(list).toHaveBeenCalledWith({ status: 'active' });
  });
  it('useAdminCreateTenant seeds job and invalidates admin tenants', async () => {
    const created = { tenant: { name: 't' }, job: { name: 'j9', state: 'queued' }, checkout_required: false };
    const create = vi.fn(async () => created);
    const { client } = fake({ admin: { tenants: { create } } });
    const qc = qcNew(); const spy = vi.spyOn(qc, 'invalidateQueries');
    const { result } = renderHook(() => useAdminCreateTenant(), { wrapper: wrap(client, qc) });
    await act(async () => { await result.current.mutateAsync({ owner_email: 'o@x.com', plan: 'pro' }); });
    expect(create).toHaveBeenCalledWith({ owner_email: 'o@x.com', plan: 'pro' });
    expect(qc.getQueryData(saasKeys.job('j9'))).toEqual(created.job);
    expect(spy).toHaveBeenCalledWith({ queryKey: saasKeys.adminTenants() });
  });
  it('useAdminInviteTenant calls invite and invalidates', async () => {
    const invite = vi.fn(async () => ({ name: 'TM-1' }));
    const { client } = fake({ admin: { tenants: { invite } } });
    const qc = qcNew(); const spy = vi.spyOn(qc, 'invalidateQueries');
    const { result } = renderHook(() => useAdminInviteTenant(), { wrapper: wrap(client, qc) });
    await act(async () => { await result.current.mutateAsync({ tenant: 't', owner_email: 'o@x.com' }); });
    expect(invite).toHaveBeenCalledWith({ tenant: 't', owner_email: 'o@x.com' });
    expect(spy).toHaveBeenCalledWith({ queryKey: saasKeys.adminTenants() });
  });
});
