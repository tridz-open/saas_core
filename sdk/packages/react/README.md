# @frappe-saaskit/react

TanStack Query v5 hooks and a provider over `@frappe-saaskit/client`.

## Install

```bash
pnpm add @frappe-saaskit/react @frappe-saaskit/client @tanstack/react-query react
```

## Provider setup

```tsx
import { createClient } from '@frappe-saaskit/client';
import { SaasProvider, usePlans } from '@frappe-saaskit/react';

const client = createClient({ baseUrl: '' /* same-origin; otherwise the site origin, e.g. 'https://app.example.com' */ });

export const App = () => (
  <SaasProvider client={client}>{/* optional: queryClient={myQueryClient} */}
    <Plans />
  </SaasProvider>
);
const Plans = () => { const { data, isLoading } = usePlans(); /* ... */ };
```

`<SaasProvider client queryClient?>` also mounts a `QueryClientProvider` (a default `QueryClient` is created if you do not pass one). Hooks throw outside the provider.

## Hooks

Query hooks return `UseQueryResult<T>`; mutation hooks return `UseMutationResult<Result, Error, Variables>`. Types come from `@frappe-saaskit/client`. Tenant ids are `Tenant.name`; job ids are `Job.name`.

| Name | Args | Returns / variables | Notes |
|---|---|---|---|
| `useSaasClient` | - | `SaasClient` | raw client |
| `usePlans` | - | query `Plan[]` | |
| `useTemplates` | - | query `Template[]` | |
| `useMe` | - | query `Me` | |
| `useSignup` | - | mutation `Me`, vars `SignupRequest` | clears all saas queries, sets `me` |
| `useLogin` | - | mutation `Me`, vars `LoginRequest` | clears all saas queries, sets `me` |
| `useLogout` | - | mutation `void`, vars `void` | clears all saas queries |
| `useRequestPasswordReset` | - | mutation `void`, vars `email: string` | |
| `useConfirmPasswordReset` | - | mutation `void`, vars `{ key, new_password }` | |
| `useTenants` | - | query `Tenant[]` | |
| `useTenant` | `id` | query `Tenant` | disabled if id empty |
| `useTenantLive` | `id` | query `Tenant` (live) | subscribes `{tenantId}`; events refetch; closes when status in `STABLE_TENANT_STATUSES` |
| `useSuspendStatus` | `tenantId` | query `SuspendStatus` | |
| `useCreateTenant` | - | mutation `{tenant, job}`, vars `TenantCreateRequest` | seeds tenant + job cache |
| `useRequestDeleteTenant` | - | mutation `Job`, vars `{ id, confirm_slug, reason? }` | seeds job; `useDeleteTenant` is a deprecated alias |
| `useChangePlan` | - | mutation `Job`, vars `{ id, plan }` | seeds job; invalidates tenant + subscription |
| `useOpenLoginLink` | - | mutation `Link`, vars `tenantId` | |
| `useBackups` | `tenantId` | query `Backup[]` | |
| `useCreateBackup` | - | mutation `Job`, vars `tenantId` | seeds job; invalidates backups |
| `useBackupDownloadLink` | - | mutation `Link`, vars `{ tenantId, backup, part }` | |
| `useCheckout` | - | mutation `CheckoutSession`, vars `CheckoutRequest` | calls `checkout.createSession`; redirect yourself |
| `useSubscription` | `tenantId` | query `Subscription` | |
| `useCancelSubscription` | - | mutation `CancelResult`, vars `{ tenantId, reason? }` | `billing.cancel`; refunds unused days |
| `usePreviewCancel` | - | mutation `CancelPreview`, vars `tenantId` | read-only: days used, refund, stop time |
| `usePreviewChange` | - | mutation `ChangePreview`, vars `{ tenantId, plan }` | read-only prorated plan-change preview |
| `usePaymentMethodsPortalLink` | - | mutation `Link`, vars `{ tenant?, return_url? } \| void` | |
| `useInvoices` | `tenantId?` | query `Invoice[]` | |
| `useInvoice` | `id` | query `Invoice` | |
| `useInvoicePdf` | - | mutation `Blob`, vars `id` | |
| `useJob` | `id` | query `Job` (live) | subscribes `{jobId}`; events refetch; closes when `state` in `TERMINAL_JOB_STATES`; then invalidates tenants/tenant/backups |
| `useJobs` | `tenantId?` | query `Job[]` | |
| `useSupportTickets` | - | query `SupportTicket[]` | |
| `useSupportTicket` | `id` | query `SupportTicket` | |
| `useCreateSupportTicket` | - | mutation `SupportTicket`, vars `CreateTicketInput` | |
| `useReplyToTicket` | - | mutation `SupportTicket`, vars `{ ticket, body }` | |
| `useUpdateProfile` | - | mutation `Me`, vars `ProfileUpdate` | sets `me` query data |
| `useApiTokens` | - | query `ApiToken[]` | |
| `useCreateApiToken` | - | mutation `ApiTokenCreated`, vars `{ label, tenant?, expires_at? }` | secret only in mutation result; invalidates list |
| `useRevokeApiToken` | - | mutation `void`, vars `token` | invalidates list |
| `useTeamMembers` | `tenantId` | query `TeamMember[]` | |
| `useInviteMember` | - | mutation `TeamMember`, vars `{ tenantId, email, role }` | invalidates team list |
| `useRevokeInvite` | - | mutation `void`, vars `{ tenantId, invite }` | invalidates team list |
| `useChangeMemberRole` | - | mutation `TeamMember`, vars `{ tenantId, member, role }` | invalidates team list |
| `useRemoveMember` | - | mutation `void`, vars `{ tenantId, member }` | invalidates team list |
| `useAcceptInvite` | - | mutation `TeamMember`, vars `token` | invalidates tenants + me |
| `useRestoreBackup` | - | mutation `RestoreApproval`, vars `{ tenantId, backup }` | returns pending approval, not a job |

## Renames in v0.2 (contract alignment)

- `useDeleteTenant` -> `useRequestDeleteTenant` (vars `{ id, confirm_slug, reason? }`, returns `Job`); old name kept as deprecated alias.
- `useRestoreBackup` removed (no restore endpoint in the contract).
- `useCheckout` now calls `client.checkout.createSession` with `CheckoutRequest`.
- `useSubscription(tenantId)` now takes a tenant id; `useCancelSubscription` vars are `{ tenantId, ... }`.
- `useCreateBackup` returns a `Job` (was `{backup, job}`).
- Ids: tenants use `Tenant.name`, jobs use `Job.name`; `Job.status` is now `Job.state`.
- `useJob`/`useTenantLive` consume contract events (`job.updated`, `tenant.updated`, `resync`, ...) by refetching, not by trusting payloads.
- Query keys: `job(id)` is now `['saas','job',id]`; added `templates`, `suspendStatus`, `invoice`, `jobs`, `ticket`; `subscription(tenantId)`, `invoices(tenantId?)`.

## Changes in v0.3

- New hooks: `useUpdateProfile`, `useApiTokens`, `useCreateApiToken`, `useRevokeApiToken`, `useTeamMembers`, `useInviteMember`, `useRevokeInvite`, `useChangeMemberRole`, `useRemoveMember`, `useAcceptInvite`, `useRestoreBackup`.
- `useRestoreBackup` is back and returns a pending `RestoreApproval` (not a `Job`); no job is seeded.
- `Event.data` is now typed (discriminated union `SaasEvent`); `useJob`/`useTenantLive` still just refetch on events.
- Client new methods: `me.update`, `apiTokens.list/create/revoke`, `team.list/invite/revokeInvite/changeRole/remove/acceptInvite`, `backups.restore`.
- Query keys added: `apiTokens()`, `team(tenantId)`.

## Changes in v0.4

- Contract 1.2.0. New hooks: `useRequestEmailVerification`, `useConfirmEmail` (invalidates `me`), `useCloseTicket({ticket})` (updates ticket cache).
- Client new methods: `auth.requestEmailVerification()`, `auth.confirmEmail(key)`, `support.close(ticket)`. Vue composables mirror the hooks.

## Query keys

`saasKeys`: `all`, `plans`, `templates`, `me`, `apiTokens()`, `team(tenantId)`, `tenants`, `tenant(id)`, `suspendStatus(id)`, `backups(tenantId)`, `invoices(tenantId?)`, `invoice(id)`, `subscription(tenantId)`, `subscriptions()`, `jobs(tenantId?)`, `job(id)`, `support`, `ticket(id)`.

```ts
queryClient.invalidateQueries({ queryKey: saasKeys.tenants() });
```
