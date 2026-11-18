# Security policy

frappe-saaskit-sdk is early alpha software, provided "as is" without warranty of any kind (see [LICENSE](LICENSE)).

## Reporting a vulnerability

Please report security issues **privately**, not in public issues. Use GitHub's private vulnerability reporting: open the repository's **Security** tab and click **Report a vulnerability**
(<https://github.com/tridz-open/frappe-saaskit-sdk/security/advisories/new>).

Include the version or tag, what you did, what you expected and what happened. We aim to acknowledge within 7 days.

## Scope

The SDK never persists credentials or CSRF tokens to storage. Cookie-session and API-key auth are supported.
