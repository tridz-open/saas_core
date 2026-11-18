# @frappe-saaskit/vue

> **Experimental.** This package has no consumers yet and has not been verified against a live saas_core. It prints a one-time console notice. Expect breaking changes.
Vue 3 composables (peer dep: `vue` only). Tenant ids are `Tenant.name`, job ids are `Job.name`.
```ts
// app setup: app.provide(SAAS_CLIENT_KEY, client)   or in a component: provideSaas(client)
const { data: tenants, loading, error, refetch } = useTenants();
const { mutate: create } = useCreateTenant();
const job = useJob(jobId);   // refetches jobs.get on each event; stops at a terminal state
```
Queries (`{ data, error, loading, refetch, reload }`; id args may be refs/getters):
`usePlans, useTemplates, useMe, useTenants, useTenant(id), useSuspendStatus(id), useBackups(tenantId), useSubscription(tenantId), useInvoices, useInvoice(id), useJobs, useSupportTickets, useSupportTicket(id)`.

Live queries: `useJob(id)` (stops at `TERMINAL_JOB_STATES`), `useTenantLive(id)` (stops at `STABLE_TENANT_STATUSES`). Both close on scope dispose / id change.

Mutations (`{ mutate, submit, data, error, loading }`):
`useSignup, useLogin, useLogout, useRequestPasswordReset, useConfirmPasswordReset, useCreateTenant, useDuplicateTenant(id, {slug}), useRequestDeleteTenant(id, {confirm_slug, reason?}), useOpenLoginLink(id), useChangePlan(id, plan), useCreateBackup(tenantId), useBackupDownloadLink(tenantId, backup, part), useCheckout (checkout.createSession), useSubscriptionPortalLink(tenantId, returnUrl?), useCancelSubscription(tenantId, opts?), usePaymentMethodsPortalLink, useInvoicePdf(id), useCreateSupportTicket, useReplyToTicket(ticket, body)`.

More: `useApiTokens`, `useTeamMembers(tenantId)` (queries); `useUpdateProfile, useCreateApiToken, useRevokeApiToken, useInviteMember(tenantId, {email, role}), useRevokeInvite, useChangeMemberRole, useRemoveMember, useAcceptInvite(token), useRestoreBackup(tenantId, backup)` (mutations; restore resolves to a pending `RestoreApproval`, not a job).
There is no cache: after a list-changing mutation call the list query's `refetch()`, e.g. `await invite(t, {...}); await members.refetch()`.

Changes: `useDeleteTenant` is a deprecated alias of `useRequestDeleteTenant`; `useSubscription` now takes a tenant id.
`reload`/`submit` are aliases for frappe-ui `createResource`-style code.
