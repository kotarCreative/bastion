#!/bin/sh
set -eu

# Smoke test for the AWS test deployment.
#
# Required environment variables:
#   BASTION_URL                 http:// or https:// URL (from Terraform output bastion_url)
#   BASTION_TESTER_TOKEN        Tester credential printed by init-forge.sh
#   BASTION_OBSERVER_TOKEN      Observer credential printed by init-forge.sh
#
# Optional:
#   BASTION_INSECURE_TLS=0      Disable TLS verification (default: 0, verify certificates)

base_url="${BASTION_URL:?BASTION_URL is required}"
tester_token="${BASTION_TESTER_TOKEN:?BASTION_TESTER_TOKEN is required}"
observer_token="${BASTION_OBSERVER_TOKEN:?BASTION_OBSERVER_TOKEN is required}"
insecure="${BASTION_INSECURE_TLS:-0}"

curl_tls=""
git_ssl="true"
if [ "$insecure" = "1" ]; then
    curl_tls="-k"
    git_ssl="false"
fi

temporary="$(mktemp -d)"
trap 'rm -rf "$temporary"' EXIT INT TERM

curl $curl_tls --fail --silent --show-error "$base_url/healthz" >/dev/null
curl $curl_tls --fail --silent --show-error \
    -H "Authorization: Bearer $tester_token" \
    "$base_url/api/v1/session" >/dev/null

tester_auth="$(printf '%s' "tester:$tester_token" | base64 | tr -d '\n')"
observer_auth="$(printf '%s' "observer:$observer_token" | base64 | tr -d '\n')"

git -c http.sslVerify="$git_ssl" \
    -c "http.extraHeader=Authorization: Basic $tester_auth" \
    clone -q "$base_url/tester.git" "$temporary/tester"
git -c http.sslVerify="$git_ssl" \
    -c "http.extraHeader=Authorization: Basic $observer_auth" \
    clone -q "$base_url/observer.git" "$temporary/observer"

test -f "$temporary/tester/private/secret.txt"
test ! -e "$temporary/observer/private/secret.txt"
test -f "$temporary/observer/shared/README.md"

git -C "$temporary/tester" config user.name "Bastion Tester"
git -C "$temporary/tester" config user.email "tester@bastion.invalid"
printf '%s\n' "deployed smoke $(date -u +%s)" >"$temporary/tester/shared/deployed-smoke.txt"
git -C "$temporary/tester" add shared/deployed-smoke.txt
git -C "$temporary/tester" commit -q -m "Exercise deployed receive path"
git -C "$temporary/tester" \
    -c http.sslVerify="$git_ssl" \
    -c "http.extraHeader=Authorization: Basic $tester_auth" \
    push -q origin main

attempt=0
while [ "$attempt" -lt 30 ]; do
    attempt=$((attempt + 1))
    git -C "$temporary/observer" \
        -c http.sslVerify="$git_ssl" \
        -c "http.extraHeader=Authorization: Basic $observer_auth" \
        fetch -q origin main
    if git -C "$temporary/observer" cat-file -e \
        origin/main:shared/deployed-smoke.txt 2>/dev/null; then
        break
    fi
    sleep 2
done

git -C "$temporary/observer" cat-file -e origin/main:shared/deployed-smoke.txt
printf '%s\n' "Bastion AWS deployment smoke test passed."
