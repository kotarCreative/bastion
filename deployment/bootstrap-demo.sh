#!/bin/sh
set -eu

data_dir="${BASTION_DATA_DIR:-/var/lib/bastion}"
root="${BASTION_ROOT:-$data_dir/forge}"
source_repo="$data_dir/source"

if [ -n "${BASTION_DATABASE:-}" ] && [ -n "${BASTION_ADMIN_DATABASE:-}" ]; then
    python deployment/provision-postgres.py "$root"
    unset BASTION_ADMIN_DATABASE
fi

if [ ! -d "$source_repo/.git" ]; then
    mkdir -p "$source_repo/shared" "$source_repo/private"
    git init -q -b main "$source_repo"
    git -C "$source_repo" config user.name "Bastion Test Seed"
    git -C "$source_repo" config user.email "seed@bastion.invalid"
    printf '%s\n' "Shared test content" >"$source_repo/shared/README.md"
    printf '%s\n' "Visible only to the tester view" >"$source_repo/private/secret.txt"
    git -C "$source_repo" add -A
    GIT_AUTHOR_DATE="1700000000 +0000" \
    GIT_COMMITTER_DATE="1700000000 +0000" \
        git -C "$source_repo" commit -q -m "Initial test repository"

fi

if ! python -c 'import sys; from pathlib import Path; from state import load_state_meta; sys.exit(0 if load_state_meta(Path(sys.argv[1])).get("repository_state") == "ready" else 1)' "$root" 2>/dev/null; then
    # shellcheck disable=SC2086
    set -- "$root" "$source_repo" --branch main --object-store "${BASTION_OBJECT_STORE:-filesystem}"
    if [ -n "${BASTION_DATABASE:-}" ]; then
        set -- "$@" --database "${BASTION_DATABASE}"
    fi
    python forge.py init "$@"
fi

python deployment/bootstrap_demo.py "$root"
