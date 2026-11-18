# saas_core documentation index

| Doc | Purpose |
| --- | --- |
| [STATUS.md](STATUS.md) | What is verified live, experimental, or removed; the `saas_core_experimental` flag and site_config keys. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the control plane behaves at runtime (diagrams in `assets/`). |
| [TEMPLATES.md](TEMPLATES.md) | The `Template` DocType: fields, validation, and warnings for tenant site recipes. |
| [gateways/README.md](gateways/README.md) | Payment-gateway layer overview: request flow, built-in gateways and their status. |
| [gateways/CONTRACT.md](gateways/CONTRACT.md) | The exact `Gateway` contract: every method, event kinds, capabilities, money, webhook security, idempotency. |
| [gateways/ADDING_A_GATEWAY.md](gateways/ADDING_A_GATEWAY.md) | Step-by-step guide to add a gateway from your own Frappe app via the `saas_core_gateways` hook. |
| [gateways/AGENT_PROMPT.md](gateways/AGENT_PROMPT.md) | Copy-paste prompt that tells a coding agent to add gateway X safely. |

Repo-root docs: [../README.md](../README.md), [../ARCHITECTURE.md](../ARCHITECTURE.md) (module contract),
[../DEPLOY.md](../DEPLOY.md) (Frappe Cloud runbook), [../SECURITY.md](../SECURITY.md), [../CONTRIBUTING.md](../CONTRIBUTING.md),
[../CHANGELOG.md](../CHANGELOG.md).
