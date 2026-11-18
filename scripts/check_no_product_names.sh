#!/usr/bin/env bash
# Fails if a product/customer name leaks into this generic package. Run from the repo root (CI does).
set -euo pipefail
if rg -i '\bury\b' --glob '!.git' --glob '!.venv' --glob '!scripts/check_no_product_names.sh' --glob '!LICENSE' .; then
  echo "error: product-specific name found (see matches above)" >&2
  exit 1
fi
echo "ok: no product-specific names"
