---
"@frappe-saaskit/client": minor
"@frappe-saaskit/react": minor
"@frappe-saaskit/vue": minor
"@frappe-saaskit/node": minor
"@frappe-saaskit/mock": minor
---

OSS alpha cleanup. Removed the `bearer` auth strategy (saas_core rejects it) and the Node `scheme` option. The mock now supports Razorpay only (Stripe and Paddle paths removed). `@frappe-saaskit/vue` and `@frappe-saaskit/node` are marked experimental with a one-time console notice. README gains a "Verified against live saas_core" matrix.
