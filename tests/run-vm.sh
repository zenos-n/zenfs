#!/usr/bin/env bash
set -euo pipefail
if ! systemd-detect-virt --vm --quiet; then
  printf '%s\n' 'ZenFS runtime tests require a VM; refusing host execution.' >&2
  exit 1
fi
root="$(dirname -- "$(readlink -f -- "$0")")"
if [[ ! -d "$root/recovered" ]]; then
  root="$(dirname -- "$root")"
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$root/tools:$root/recovered/packages/zenfsctl:$root/recovered/packages/app-index:$root/recovered/packages/nautilus-apps"
export ZENFSCTL_SCRIPT="$root/recovered/packages/zenfsctl/zenfsctl.py"
python="${ZENFS_TEST_PYTHON:-python3}"
"$python" -B -m unittest discover -s "$root/recovered/tests/zenfs" -v
"$python" -B -m unittest discover -s "$root/recovered/tests/app-index" -v
"$python" -B -m unittest discover -s "$root/tests" -p 'test_*.py' -v
