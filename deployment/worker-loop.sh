#!/bin/sh
set -eu

root="${BASTION_ROOT:-/var/lib/bastion/forge}"
interval="${BASTION_WORKER_INTERVAL_SECONDS:-2}"
ready_marker="${BASTION_DATA_DIR:-/var/lib/bastion}/worker-ready"

while :; do
    if python forge.py work "$root" --limit 32; then
        date +%s >"$ready_marker"
    else
        echo "bastion: projection worker pass failed" >&2
    fi
    sleep "$interval"
done
