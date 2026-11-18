import { getCurrentScope, inject, onScopeDispose, provide, ref, shallowRef, toValue, watch } from 'vue';
import type { InjectionKey, MaybeRefOrGetter, Ref } from 'vue';
import { TERMINAL_JOB_STATES, STABLE_TENANT_STATUSES } from '@frappe-saaskit/client';
import type { AdminTenantCreateRequest, AdminTenantInviteRequest, SaasClient, Job, Tenant, TenantCreateRequest, CheckoutRequest, CreateTicketInput, SignupRequest, LoginRequest, BackupPart, ListParams, TenantStatus, ProfileUpdate, CreateTokenInput, TeamRole } from '@frappe-saaskit/client';

export const SAAS_CLIENT_KEY: InjectionKey<SaasClient> = Symbol('saaskit-client');
let noticed = false;
function experimentalNotice(): void {
  if (noticed) return;
  noticed = true;
  console.warn('[frappe-saaskit] @frappe-saaskit/vue is EXPERIMENTAL: it has no consumers and is only tested against mocked clients.');
}
export function provideSaas(client: SaasClient): SaasClient { experimentalNotice(); provide(SAAS_CLIENT_KEY, client); return client; }
/** For app.runWithContext / createApp use: app.provide(SAAS_CLIENT_KEY, client). */
export function useSaasClient(): SaasClient {
  experimentalNotice();
  const c = inject(SAAS_CLIENT_KEY, null);
  if (!c) throw new Error('useSaasClient: no client provided; call provideSaas(client) in an ancestor');
  return c;
}

export interface QueryResult<T> { data: Ref<T | undefined>; error: Ref<Error | null>; loading: Ref<boolean>; refetch: () => Promise<void>; /** frappe-ui alias */ reload: () => Promise<void>; }
export interface MutationResult<A extends unknown[], R> { mutate: (...a: A) => Promise<R>; data: Ref<R | undefined>; error: Ref<Error | null>; loading: Ref<boolean>; /** frappe-ui alias */ submit: (...a: A) => Promise<R>; }

const toErr = (e: unknown): Error => (e instanceof Error ? e : new Error(String(e)));

function query<T>(fetcher: () => Promise<T>, deps?: () => unknown, enabled?: () => boolean): QueryResult<T> {
  const data = shallowRef<T | undefined>(undefined) as Ref<T | undefined>;
  const error = shallowRef<Error | null>(null);
  const loading = ref(false);
  let seq = 0;
  const refetch = async () => {
    if (enabled && !enabled()) return;
    const my = ++seq; loading.value = true;
    try { const r = await fetcher(); if (my === seq) { data.value = r; error.value = null; } }
    catch (e) { if (my === seq) error.value = toErr(e); }
    finally { if (my === seq) loading.value = false; }
  };
  if (deps) watch(deps, () => { void refetch(); }, { immediate: true }); else void refetch();
  return { data, error, loading, refetch, reload: refetch };
}

function mutation<A extends unknown[], R>(fn: (...a: A) => Promise<R>): MutationResult<A, R> {
  const data = shallowRef<R | undefined>(undefined) as Ref<R | undefined>;
  const error = shallowRef<Error | null>(null);
  const loading = ref(false);
  const mutate = async (...a: A): Promise<R> => {
    loading.value = true; error.value = null;
    try { const r = await fn(...a); data.value = r; return r; }
    catch (e) { error.value = toErr(e); throw error.value; }
    finally { loading.value = false; }
  };
  return { mutate, data, error, loading, submit: mutate };
}

type Id = MaybeRefOrGetter<string | undefined>;
const idq = <T>(id: Id, f: (v: string) => Promise<T>) => query(() => f(toValue(id) as string), () => toValue(id), () => !!toValue(id));

export const usePlans = (p?: { currency?: string; interval?: 'month' | 'year' }) => { const c = useSaasClient(); return query(() => c.plans.list(p as never)); };
export const useTemplates = (p?: ListParams) => { const c = useSaasClient(); return query(() => c.templates.list(p)); };
export const usePublicConfig = () => { const c = useSaasClient(); return query(() => c.config.get()); };
export const useAdminTenants = (p?: ListParams & { status?: TenantStatus; owner_email?: string }) => { const c = useSaasClient(); return query(() => c.admin.tenants.list(p)); };
export const useAdminCreateTenant = () => { const c = useSaasClient(); return mutation((i: AdminTenantCreateRequest) => c.admin.tenants.create(i)); };
export const useAdminInviteTenant = () => { const c = useSaasClient(); return mutation((i: AdminTenantInviteRequest) => c.admin.tenants.invite(i)); };
export const useMe = () => { const c = useSaasClient(); return query(() => c.me.get()); };
export const useSignup = () => { const c = useSaasClient(); return mutation((i: SignupRequest) => c.auth.signup(i)); };
export const useLogin = () => { const c = useSaasClient(); return mutation((i: LoginRequest) => c.auth.login(i)); };
export const useLogout = () => { const c = useSaasClient(); return mutation(() => c.auth.logout()); };
export const useRequestPasswordReset = () => { const c = useSaasClient(); return mutation((email: string) => c.auth.requestPasswordReset(email)); };
export const useConfirmPasswordReset = () => { const c = useSaasClient(); return mutation((i: { key: string; new_password: string }) => c.auth.confirmPasswordReset(i)); };

export const useTenants = (p?: ListParams & { status?: TenantStatus }) => { const c = useSaasClient(); return query(() => c.tenants.list(p)); };
export const useSlugAvailability = (slug: Id) => { const c = useSaasClient(); return idq(slug, (v) => c.tenants.checkSlug(v)); };
export const useTenant = (id: Id) => { const c = useSaasClient(); return idq(id, (v) => c.tenants.get(v)); };
export const useCreateTenant = () => { const c = useSaasClient(); return mutation((i: TenantCreateRequest) => c.tenants.create(i)); };
export const useRequestDeleteTenant = () => { const c = useSaasClient(); return mutation((id: string, i: { confirm_slug: string; reason?: string }) => c.tenants.requestDelete(id, i)); };
/** @deprecated use useRequestDeleteTenant */
export const useDeleteTenant = useRequestDeleteTenant;
export const useSuspendStatus = (id: Id) => { const c = useSaasClient(); return idq(id, (v) => c.tenants.suspendStatus(v)); };
export const useOpenLoginLink = () => { const c = useSaasClient(); return mutation((id: string) => c.tenants.openLoginLink(id)); };
export const useChangePlan = () => { const c = useSaasClient(); return mutation((id: string, plan: string) => c.tenants.changePlan(id, plan)); };

export const useBackups = (tenantId: Id, p?: ListParams) => { const c = useSaasClient(); return idq(tenantId, (v) => c.backups.list(v, p)); };
export const useCreateBackup = () => { const c = useSaasClient(); return mutation((tenantId: string) => c.backups.create(tenantId)); };
export const useBackupDownloadLink = () => { const c = useSaasClient(); return mutation((tenantId: string, backup: string, part: BackupPart) => c.backups.downloadLink(tenantId, backup, part)); };

export const useCheckout = () => { const c = useSaasClient(); return mutation((i: CheckoutRequest) => c.checkout.createSession(i)); };
export const useSubscription = (tenantId: Id) => { const c = useSaasClient(); return idq(tenantId, (v) => c.subscription.get(v)); };
export const useCancelSubscription = () => { const c = useSaasClient(); return mutation((tenantId: string, i?: { reason?: string }) => c.billing.cancel(tenantId, i)); };
export const usePreviewCancel = () => { const c = useSaasClient(); return mutation((tenantId: string) => c.billing.previewCancel(tenantId)); };
export const usePreviewChange = () => { const c = useSaasClient(); return mutation((tenantId: string, plan: string) => c.billing.previewChange(tenantId, plan)); };
export const usePaymentMethodsPortalLink = () => { const c = useSaasClient(); return mutation((i?: { tenant?: string; return_url?: string }) => c.paymentMethods.portalLink(i)); };

export const useInvoices = (p?: ListParams & { tenant?: string }) => { const c = useSaasClient(); return query(() => c.invoices.list(p)); };
export const useInvoice = (id: Id) => { const c = useSaasClient(); return idq(id, (v) => c.invoices.get(v)); };
export const useInvoicePdf = () => { const c = useSaasClient(); return mutation((id: string) => c.invoices.pdf(id)); };

export const useJobs = (p?: ListParams & { tenant?: string }) => { const c = useSaasClient(); return query(() => c.jobs.list(p)); };
export const useSupportTickets = (p?: ListParams) => { const c = useSaasClient(); return query(() => c.support.list(p)); };
export const useSupportTicket = (id: Id) => { const c = useSaasClient(); return idq(id, (v) => c.support.get(v)); };
export const useCreateSupportTicket = () => { const c = useSaasClient(); return mutation((i: CreateTicketInput) => c.support.create(i)); };
export const useReplyToTicket = () => { const c = useSaasClient(); return mutation((ticket: string, body: string) => c.support.reply(ticket, body)); };

/** Shared live loop: subscribe, refetch on every event, stop once `done(data)`; closed on scope dispose / id change. */
function live<T>(c: SaasClient, id: Id, key: 'jobId' | 'tenantId', fetcher: (v: string) => Promise<T>, done: (d: T) => boolean): QueryResult<T> {
  const q = idq(id, fetcher);
  let sub: { close(): void } | null = null;
  const stop = () => { sub?.close(); sub = null; };
  watch(() => toValue(id), (v) => {
    stop();
    if (!v) return;
    sub = c.subscribe({ [key]: v } as { jobId: string } | { tenantId: string }, async () => {
      await q.refetch();
      if (q.data.value !== undefined && done(q.data.value)) stop();
    }, (e) => { q.error.value = toErr(e); });
  }, { immediate: true });
  if (getCurrentScope()) onScopeDispose(stop);
  return q;
}
/** Live job: initial fetch, refetch via jobs.get on each event, stops at a terminal state. */
export function useJob(id: Id): QueryResult<Job> {
  const c = useSaasClient();
  return live(c, id, 'jobId', (v) => c.jobs.get(v), (j) => TERMINAL_JOB_STATES.includes(j.state));
}
/** Live tenant: refetch via tenants.get on each event, stops at a stable status. */
export function useTenantLive(id: Id): QueryResult<Tenant> {
  const c = useSaasClient();
  return live(c, id, 'tenantId', (v) => c.tenants.get(v), (t) => STABLE_TENANT_STATUSES.includes(t.status));
}

export const useUpdateProfile = () => { const c = useSaasClient(); return mutation((i: ProfileUpdate) => c.me.update(i)); };
export const useApiTokens = (p?: ListParams) => { const c = useSaasClient(); return query(() => c.apiTokens.list(p)); };
export const useCreateApiToken = () => { const c = useSaasClient(); return mutation((i: CreateTokenInput) => c.apiTokens.create(i)); };
export const useRevokeApiToken = () => { const c = useSaasClient(); return mutation((token: string) => c.apiTokens.revoke(token)); };
export const useTeamMembers = (tenantId: Id, p?: ListParams) => { const c = useSaasClient(); return idq(tenantId, (v) => c.team.list(v, p)); };
export const useInviteMember = () => { const c = useSaasClient(); return mutation((tenantId: string, i: { email: string; role: TeamRole }) => c.team.invite(tenantId, i)); };
export const useRevokeInvite = () => { const c = useSaasClient(); return mutation((tenantId: string, invite: string) => c.team.revokeInvite(tenantId, invite)); };
export const useChangeMemberRole = () => { const c = useSaasClient(); return mutation((tenantId: string, member: string, role: TeamRole) => c.team.changeRole(tenantId, member, role)); };
export const useRemoveMember = () => { const c = useSaasClient(); return mutation((tenantId: string, member: string) => c.team.remove(tenantId, member)); };
export const useRequestEmailVerification = () => { const c = useSaasClient(); return mutation(() => c.auth.requestEmailVerification()); };
export const useConfirmEmail = () => { const c = useSaasClient(); return mutation((key: string) => c.auth.confirmEmail(key)); };
export const useCloseTicket = () => { const c = useSaasClient(); return mutation((ticket: string) => c.support.close(ticket)); };
export const useAcceptInvite = () => { const c = useSaasClient(); return mutation((token: string) => c.team.acceptInvite(token)); };
/** Resolves to a pending RestoreApproval (not a job). */
export const useRestoreBackup = () => { const c = useSaasClient(); return mutation((tenantId: string, backup: string) => c.backups.restore(tenantId, backup)); };
