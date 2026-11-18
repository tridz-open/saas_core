app_name = "saas_core"
app_title = "SaaS Core"
app_publisher = "Tridz Technologies"
app_description = "Generic SaaS control plane"
app_email = ""
app_license = "MIT"

fixtures = [
    {"dt": "Role", "filters": [["name", "in", ["SaaS Admin", "SaaS Customer"]]]},
    {"dt": "Custom Field", "filters": [["name", "in", ["User-email_verified"]]]},
    {"dt": "Email Template", "filters": [["name", "in", ["saas-tenant-ready", "saas-tenant-setup", "saas-invoice-paid", "saas-payment-failed", "saas-dunning", "saas-beta-invite", "saas-cancellation", "saas-refund-processed", "saas-renewal-receipt"]]]},
]

permission_query_conditions = {
    "Tenant": "saas_core.permissions.tenant_query",
    "Subscription": "saas_core.permissions.subscription_query",
    "Invoice": "saas_core.permissions.invoice_query",
    "Provision Job": "saas_core.permissions.job_query",
    "Support Ticket": "saas_core.permissions.ticket_query",
}
has_permission = {
    "Tenant": "saas_core.permissions.has_tenant_permission",
    "Subscription": "saas_core.permissions.has_tenant_child_permission",
    "Invoice": "saas_core.permissions.has_tenant_child_permission",
    "Provision Job": "saas_core.permissions.has_tenant_child_permission",
    "Support Ticket": "saas_core.permissions.has_ticket_permission",
}
doc_events = {"Provision Job": {"on_update": "saas_core.events.on_job_update"}}
scheduler_events = {
    "cron": {
        "* * * * *": ["saas_core.billing.processor.sweep_received_events",
                      "saas_core.billing.cancellation.sweep_due"],
        "*/15 * * * *": ["saas_core.provisioning.reconcile.run",
                         "saas_core.billing.plan_change.sweep_due",
                         "saas_core.provisioning.runner.run_due_scheduled"],
        "*/30 * * * *": ["saas_core.jobs.keepalive"],
        "0 * * * *": ["saas_core.notifications.purge.purge_sent_setup_links"],
    },
    "daily": ["saas_core.billing.dunning.run_daily", "saas_core.jobs.daily_tenant_backups",
              "saas_core.billing.processor.issue_due_invoices",
              "saas_core.billing.expiry.sweep_expired"],
}
before_install = "saas_core.install.before_install"
after_install = "saas_core.install.after_install"

# Bearer API tokens (see saas_core/token_auth.py); Frappe `token key:secret` auth is unaffected
auth_hooks = ["saas_core.token_auth.validate"]
