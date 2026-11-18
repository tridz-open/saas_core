import { useEffect } from 'react';
import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { TERMINAL_JOB_STATES, STABLE_TENANT_STATUSES } from '@frappe-saaskit/client';
import type {
  ApiToken, ApiTokenCreated, CreateTokenInput, ProfileUpdate, RestoreApproval, TeamMember, TeamRole,
  Backup, BackupPart, CheckoutRequest, CheckoutSession, CreateTicketInput, Invoice, Job, Link, LoginRequest, Me, Plan,
  AdminTenantCreateRequest, AdminTenantInviteRequest, PublicConfig, SignupRequest, SlugAvailability, SuspendStatus, SupportTicket, Subscription, CancelResult, CancelPreview, ChangePreview, Template, Tenant, TenantCreateRequest, TenantCreated,
} from '@frappe-saaskit/client';
import { useSaasClient } from './provider';
import { saasKeys } from './keys';

const useC = useSaasClient;

// ---- queries
export const usePlans = () => { const c = useC(); return useQuery<Plan[]>({ queryKey: saasKeys.plans(), queryFn: () => c.plans.list() }); };
export const useTemplates = () => { const c = useC(); return useQuery<Template[]>({ queryKey: saasKeys.templates(), queryFn: () => c.templates.list() }); };
export const usePublicConfig = () => { const c = useC(); return useQuery<PublicConfig>({ queryKey: saasKeys.config(), queryFn: () => c.config.get() }); };
export const useAdminTenants = (p?: { status?: Tenant['status']; owner_email?: string }) => { const c = useC(); return useQuery<Tenant[]>({ queryKey: [...saasKeys.adminTenants(), p ?? {}], queryFn: () => c.admin.tenants.list(p) }); };
export const useMe = () => { const c = useC(); return useQuery<Me>({ queryKey: saasKeys.me(), queryFn: () => c.me.get() }); };
export const useTenants = () => { const c = useC(); return useQuery<Tenant[]>({ queryKey: saasKeys.tenants(), queryFn: () => c.tenants.list() }); };
export const useSlugAvailability = (slug: string) => { const c = useC(); return useQuery<SlugAvailability>({ queryKey: saasKeys.slugAvailability(slug), queryFn: () => c.tenants.checkSlug(slug), enabled: !!slug, staleTime: 10_000 }); };
export const useTenant = (id: string) => { const c = useC(); return useQuery<Tenant>({ queryKey: saasKeys.tenant(id), queryFn: () => c.tenants.get(id), enabled: !!id }); };
export const useSuspendStatus = (tenantId: string) => { const c = useC(); return useQuery<SuspendStatus>({ queryKey: saasKeys.suspendStatus(tenantId), queryFn: () => c.tenants.suspendStatus(tenantId), enabled: !!tenantId }); };
export const useBackups = (tenantId: string) => { const c = useC(); return useQuery<Backup[]>({ queryKey: saasKeys.backups(tenantId), queryFn: () => c.backups.list(tenantId), enabled: !!tenantId }); };
export const useInvoices = (tenantId?: string) => { const c = useC(); return useQuery<Invoice[]>({ queryKey: saasKeys.invoices(tenantId), queryFn: () => c.invoices.list(tenantId ? { tenant: tenantId } : undefined) }); };
export const useInvoice = (id: string) => { const c = useC(); return useQuery<Invoice>({ queryKey: saasKeys.invoice(id), queryFn: () => c.invoices.get(id), enabled: !!id }); };
export const useSubscription = (tenantId: string) => { const c = useC(); return useQuery<Subscription>({ queryKey: saasKeys.subscription(tenantId), queryFn: () => c.subscription.get(tenantId), enabled: !!tenantId }); };
export const useJobs = (tenantId?: string) => { const c = useC(); return useQuery<Job[]>({ queryKey: saasKeys.jobs(tenantId), queryFn: () => c.jobs.list(tenantId ? { tenant: tenantId } : undefined) }); };
export const useSupportTickets = () => { const c = useC(); return useQuery<SupportTicket[]>({ queryKey: saasKeys.support(), queryFn: () => c.support.list() }); };
export const useSupportTicket = (id: string) => { const c = useC(); return useQuery<SupportTicket>({ queryKey: saasKeys.ticket(id), queryFn: () => c.support.get(id), enabled: !!id }); };

export const useApiTokens = () => { const c = useC(); return useQuery<ApiToken[]>({ queryKey: saasKeys.apiTokens(), queryFn: () => c.apiTokens.list() }); };
export const useTeamMembers = (tenantId: string) => { const c = useC(); return useQuery<TeamMember[]>({ queryKey: saasKeys.team(tenantId), queryFn: () => c.team.list(tenantId), enabled: !!tenantId }); };

function seedJob(qc: QueryClient, job: Job) { qc.setQueryData(saasKeys.job(job.name), job); }

// ---- auth
function useSessionChange<V>(fn: (c: ReturnType<typeof useC>, v: V) => Promise<Me>) {
  const c = useC(); const qc = useQueryClient();
  return useMutation<Me, Error, V>({
    mutationFn: (v) => fn(c, v),
    onSuccess: async (me) => { await qc.cancelQueries({ queryKey: saasKeys.all }); qc.removeQueries({ queryKey: saasKeys.all }); qc.setQueryData(saasKeys.me(), me); },
  });
}
export const useSignup = () => useSessionChange<SignupRequest>((c, i) => c.auth.signup(i));
export const useLogin = () => useSessionChange<LoginRequest>((c, i) => c.auth.login(i));
export function useLogout() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () => c.auth.logout(),
    onSuccess: async () => { await qc.cancelQueries({ queryKey: saasKeys.all }); qc.removeQueries({ queryKey: saasKeys.all }); },
  });
}
export const useRequestPasswordReset = () => { const c = useC(); return useMutation<void, Error, string>({ mutationFn: (email) => c.auth.requestPasswordReset(email) }); };
export const useConfirmPasswordReset = () => { const c = useC(); return useMutation<void, Error, { key: string; new_password: string }>({ mutationFn: (i) => c.auth.confirmPasswordReset(i) }); };

export function useUpdateProfile() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<Me, Error, ProfileUpdate>({ mutationFn: (i) => c.me.update(i), onSuccess: (me) => { qc.setQueryData(saasKeys.me(), me); } });
}
export function useRequestEmailVerification() {
  const c = useC();
  return useMutation<void, Error, void>({ mutationFn: () => c.auth.requestEmailVerification() });
}
export function useConfirmEmail() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<void, Error, string>({ mutationFn: (key) => c.auth.confirmEmail(key), onSuccess: () => qc.invalidateQueries({ queryKey: saasKeys.me() }) });
}
export function useCloseTicket() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<SupportTicket, Error, string>({
    mutationFn: (ticket) => c.support.close(ticket),
    onSuccess: (t, ticket) => { qc.setQueryData(saasKeys.ticket(ticket), t); return qc.invalidateQueries({ queryKey: saasKeys.support() }); },
  });
}
export function useAdminCreateTenant() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<TenantCreated, Error, AdminTenantCreateRequest>({
    mutationFn: (i) => c.admin.tenants.create(i),
    onSuccess: (r) => { seedJob(qc, r.job); return qc.invalidateQueries({ queryKey: saasKeys.adminTenants() }); },
  });
}
export function useAdminInviteTenant() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<TeamMember, Error, AdminTenantInviteRequest>({ mutationFn: (i) => c.admin.tenants.invite(i), onSuccess: () => qc.invalidateQueries({ queryKey: saasKeys.adminTenants() }) });
}
export function useAcceptInvite() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<TeamMember, Error, string>({ mutationFn: (token) => c.team.acceptInvite(token), onSuccess: () => Promise.all([qc.invalidateQueries({ queryKey: saasKeys.tenants() }), qc.invalidateQueries({ queryKey: saasKeys.me() })]) });
}

// ---- api tokens
/** The token secret is only present in this mutation's result; it is not cached in the list. */
export function useCreateApiToken() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<ApiTokenCreated, Error, CreateTokenInput>({ mutationFn: (i) => c.apiTokens.create(i), onSuccess: () => qc.invalidateQueries({ queryKey: saasKeys.apiTokens() }) });
}
export function useRevokeApiToken() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<void, Error, string>({ mutationFn: (token) => c.apiTokens.revoke(token), onSuccess: () => qc.invalidateQueries({ queryKey: saasKeys.apiTokens() }) });
}

// ---- team
function useTeamMutation<R, V extends { tenantId: string }>(fn: (c: ReturnType<typeof useC>, v: V) => Promise<R>) {
  const c = useC(); const qc = useQueryClient();
  return useMutation<R, Error, V>({ mutationFn: (v) => fn(c, v), onSuccess: (_r, v) => qc.invalidateQueries({ queryKey: saasKeys.team(v.tenantId) }) });
}
export const useInviteMember = () => useTeamMutation<TeamMember, { tenantId: string; email: string; role: TeamRole }>((c, v) => c.team.invite(v.tenantId, { email: v.email, role: v.role }));
export const useRevokeInvite = () => useTeamMutation<void, { tenantId: string; invite: string }>((c, v) => c.team.revokeInvite(v.tenantId, v.invite));
export const useChangeMemberRole = () => useTeamMutation<TeamMember, { tenantId: string; member: string; role: TeamRole }>((c, v) => c.team.changeRole(v.tenantId, v.member, v.role));
export const useRemoveMember = () => useTeamMutation<void, { tenantId: string; member: string }>((c, v) => c.team.remove(v.tenantId, v.member));

// ---- tenants
function seedCreated(qc: QueryClient, r: TenantCreated) {
  seedJob(qc, r.job);
  qc.setQueryData(saasKeys.tenant(r.tenant.name), r.tenant);
}
export function useCreateTenant() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<TenantCreated, Error, TenantCreateRequest>({
    mutationFn: (i) => c.tenants.create(i),
    onSuccess: (r) => { seedCreated(qc, r); return qc.invalidateQueries({ queryKey: saasKeys.tenants() }); },
  });
}
export function useRequestDeleteTenant() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<Job, Error, { id: string; confirm_slug: string; reason?: string }>({
    mutationFn: ({ id, ...i }) => c.tenants.requestDelete(id, i),
    onSuccess: (job, v) => { seedJob(qc, job); return Promise.all([qc.invalidateQueries({ queryKey: saasKeys.tenant(v.id) }), qc.invalidateQueries({ queryKey: saasKeys.tenants() })]); },
  });
}
/** @deprecated use useRequestDeleteTenant */
export const useDeleteTenant = useRequestDeleteTenant;
export function useChangePlan() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<Job, Error, { id: string; plan: string }>({
    mutationFn: ({ id, plan }) => c.tenants.changePlan(id, plan),
    onSuccess: (job, v) => { seedJob(qc, job); return Promise.all([qc.invalidateQueries({ queryKey: saasKeys.tenant(v.id) }), qc.invalidateQueries({ queryKey: saasKeys.subscription(v.id) })]); },
  });
}
export const useOpenLoginLink = () => { const c = useC(); return useMutation<Link, Error, string>({ mutationFn: (id) => c.tenants.openLoginLink(id) }); };

// ---- backups
export function useCreateBackup() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<Job, Error, string>({
    mutationFn: (tenantId) => c.backups.create(tenantId),
    onSuccess: (job, tenantId) => { seedJob(qc, job); return qc.invalidateQueries({ queryKey: saasKeys.backups(tenantId) }); },
  });
}
/** Restore is approval-gated: resolves to a pending RestoreApproval, NOT a Job (nothing to seed or poll as a job). */
export const useRestoreBackup = () => {
  const c = useC();
  return useMutation<RestoreApproval, Error, { tenantId: string; backup: string }>({ mutationFn: (v) => c.backups.restore(v.tenantId, v.backup) });
};
export const useBackupDownloadLink = () => {
  const c = useC();
  return useMutation<Link, Error, { tenantId: string; backup: string; part: BackupPart }>({ mutationFn: (v) => c.backups.downloadLink(v.tenantId, v.backup, v.part) });
};

// ---- billing
export function useCheckout() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<CheckoutSession, Error, CheckoutRequest>({
    mutationFn: (i) => c.checkout.createSession(i),
    onSuccess: () => qc.invalidateQueries({ queryKey: saasKeys.subscriptions() }),
  });
}
/** Cancel now (day-based billing). Replaces the removed subscription.cancel. Call `usePreviewCancel` first to show the refund. */
export function useCancelSubscription() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<CancelResult, Error, { tenantId: string; reason?: string }>({
    mutationFn: ({ tenantId, ...i }) => c.billing.cancel(tenantId, i),
    onSuccess: (_r, v) => Promise.all([
      qc.invalidateQueries({ queryKey: saasKeys.subscription(v.tenantId) }),
      qc.invalidateQueries({ queryKey: saasKeys.invoices() }),
    ]),
  });
}
export const usePreviewCancel = () => { const c = useC(); return useMutation<CancelPreview, Error, string>({ mutationFn: (tenantId) => c.billing.previewCancel(tenantId) }); };
export const usePreviewChange = () => { const c = useC(); return useMutation<ChangePreview, Error, { tenantId: string; plan: string }>({ mutationFn: (v) => c.billing.previewChange(v.tenantId, v.plan) }); };
export const usePaymentMethodsPortalLink = () => { const c = useC(); return useMutation<Link, Error, { tenant?: string; return_url?: string } | void>({ mutationFn: (i) => c.paymentMethods.portalLink(i ?? undefined) }); };
export const useInvoicePdf = () => { const c = useC(); return useMutation<Blob, Error, string>({ mutationFn: (id) => c.invoices.pdf(id) }); };

// ---- support
export function useCreateSupportTicket() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<SupportTicket, Error, CreateTicketInput>({
    mutationFn: (i) => c.support.create(i),
    onSuccess: () => qc.invalidateQueries({ queryKey: saasKeys.support() }),
  });
}
export function useReplyToTicket() {
  const c = useC(); const qc = useQueryClient();
  return useMutation<SupportTicket, Error, { ticket: string; body: string }>({
    mutationFn: ({ ticket, body }) => c.support.reply(ticket, body),
    onSuccess: (t, v) => { qc.setQueryData(saasKeys.ticket(v.ticket), t); return qc.invalidateQueries({ queryKey: saasKeys.support() }); },
  });
}

// ---- live
/** Job query kept live via client.subscribe; events trigger a refetch (payload is not trusted). Closes once the job state is terminal. */
export function useJob(id: string) {
  const c = useC(); const qc = useQueryClient();
  const q = useQuery<Job>({ queryKey: saasKeys.job(id), queryFn: () => c.jobs.get(id), enabled: !!id });
  const terminal = !!q.data && TERMINAL_JOB_STATES.includes(q.data.state);
  const tenant = q.data?.tenant;
  useEffect(() => {
    if (!id || terminal) return;
    const sub = c.subscribe({ jobId: id }, (e) => {
      if (e.type === 'job.updated' || e.type === 'job.step' || e.type === 'resync') void qc.invalidateQueries({ queryKey: saasKeys.job(id) });
    });
    return () => sub.close();
  }, [c, qc, id, terminal]);
  useEffect(() => {
    if (!terminal) return;
    void qc.invalidateQueries({ queryKey: saasKeys.tenants() });
    if (tenant) { void qc.invalidateQueries({ queryKey: saasKeys.backups(tenant) }); void qc.invalidateQueries({ queryKey: saasKeys.tenant(tenant) }); }
  }, [qc, terminal, tenant]);
  return q;
}

/** Tenant query kept live via client.subscribe; events trigger a refetch. Closes once the status is stable. */
export function useTenantLive(id: string) {
  const c = useC(); const qc = useQueryClient();
  const q = useQuery<Tenant>({ queryKey: saasKeys.tenant(id), queryFn: () => c.tenants.get(id), enabled: !!id });
  const stable = !!q.data && STABLE_TENANT_STATUSES.includes(q.data.status);
  useEffect(() => {
    if (!id || stable) return;
    const sub = c.subscribe({ tenantId: id }, (e) => {
      if (e.type === 'tenant.updated' || e.type === 'job.updated' || e.type === 'subscription.updated' || e.type === 'resync') void qc.invalidateQueries({ queryKey: saasKeys.tenant(id) });
    });
    return () => sub.close();
  }, [c, qc, id, stable]);
  useEffect(() => { if (stable) void qc.invalidateQueries({ queryKey: saasKeys.tenants() }); }, [qc, stable]);
  return q;
}
