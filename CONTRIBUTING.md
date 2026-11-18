# Contributing

saas_core is early alpha software and is not currently accepting pull requests or outside contributions. Pull requests will be closed without review.

**Issues are welcome.** If you find a bug or a problem, please [open a GitHub issue](https://github.com/tridz-open/saas_core/issues/new/choose).

## What to include in a bug report

- The version or release tag you are using (e.g. `saas_core 0.1.0a2`).
- Your environment: OS, Python and Frappe versions, and how you installed it.
- What you did (exact commands or code), what you expected, and what happened instead.
- The full error output or stack trace.
- A minimal reproduction if you can make one.
- **Remove secrets and account data first**: tokens, session cookies, API keys, emails, hostnames, team ids.

## Security issues

Do not file security problems as public issues. See [SECURITY.md](SECURITY.md).

## Use at your own risk

Alpha software, provided "as is" without warranty of any kind (see [LICENSE](LICENSE)).

## Development setup (for reference, and for your own fork)

Pure Python; frappe is stubbed, no bench needed:

```bash
uv venv -q && uv pip install -q -e '.[test]' && .venv/bin/python -m pytest -q tests
scripts/check_no_product_names.sh          # needs ripgrep
python openapi/_gen_v1.py /tmp/v1.yaml && diff -u openapi/v1.yaml /tmp/v1.yaml   # OpenAPI must be regenerated
```

Bench tests need a Frappe bench: `BENCH_DIR=~/frappe-bench ADMIN_PASSWORD=... scripts/bench_test.sh`.
Read [ARCHITECTURE.md](ARCHITECTURE.md) first, and keep the package generic (no product or customer names).
