#!/usr/bin/env bash
# Reapplies our litellm patches (see ../FIX.md, bug #2) against whatever
# litellm version is currently installed via `uv tool install litellm`.
#
# Safe to re-run: if a patch is already applied, `patch` reports it and
# exits non-zero for that file, which this script tolerates and reports.
#
# Usage: ./apply.sh
set -uo pipefail

# Locate litellm's site-packages via the tool's own Python executable.
# uv tool dir gives e.g. ~/.local/share/uv/tools; litellm's Python is at
# <tools>/litellm/bin/python (macOS/Linux).
TOOLS_DIR="$(uv tool dir 2>/dev/null)"
if [ -z "$TOOLS_DIR" ]; then
  echo "Could not determine uv tools directory. Is uv installed?" >&2
  exit 1
fi
PYTHON_EXE="$TOOLS_DIR/litellm/bin/python"
if [ ! -x "$PYTHON_EXE" ]; then
  echo "Cannot find Python in litellm tool environment ($PYTHON_EXE). Is litellm installed with 'uv tool install litellm'?" >&2
  exit 1
fi
SITE_PACKAGES="$("$PYTHON_EXE" -c 'import litellm, os; print(os.path.dirname(os.path.dirname(litellm.__file__)))')"
if [ -z "$SITE_PACKAGES" ]; then
  echo "Could not locate litellm site-packages. Try reinstalling litellm." >&2
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
