#!/bin/sh
set -eu

data_dir="${BASTION_DATA_DIR:-/var/lib/bastion}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$data_dir"
    chown bastion:bastion "$data_dir"
    exec gosu bastion "$@"
fi

exec "$@"
