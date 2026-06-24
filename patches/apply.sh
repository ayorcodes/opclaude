#!/usr/bin/env bash
# Reapplies our litellm patches (see ../FIX.md, bug #2) against whatever
# litellm version is currently installed via `uv tool install litellm`.
#
# Safe to re-run: if a patch is already applied, `patch` reports it and
# exits non-zero for that file, which this script tolerates and reports.
#
# Usage: ./apply.sh
set -uo pipefail

SITE_PACKAGES="$(uv tool run --from litellm python -c 'import litellm, os; print(os.path.dirname(os.path.dirname(litellm.__file__)))' 2>/dev/null)"
if [ -z "$SITE_PACKAGES" ]; then
  echo "Could not locate litellm site-packages via uv. Is litellm installed with 'uv tool install litellm'?" >&2
  exit 1
fi

echo "Patching litellm install at: $SITE_PACKAGES"
cd "$SITE_PACKAGES" || exit 1

status=0
for patch_file in "$(dirname "$0")"/*.patch; do
  name="$(basename "$patch_file")"
  if patch -p1 --forward --dry-run -s -i "$patch_file" >/dev/null 2>&1; then
    patch -p1 --forward -i "$patch_file"
    echo "applied: $name"
  elif patch -p1 --forward --dry-run -s -R -i "$patch_file" >/dev/null 2>&1; then
    echo "already applied: $name"
  else
    echo "FAILED to apply (litellm internals likely changed upstream - check ../FIX.md bug #2 and re-derive the patch): $name" >&2
    status=1
  fi
done

exit $status
