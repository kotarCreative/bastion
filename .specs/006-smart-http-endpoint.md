# Smart HTTP Git Endpoint

## Status

Ready for implementation.

This specification pulls the transport boundary forward from Phase 4 as an
experimental local endpoint. It lets the modeled Git endpoint be exercised
over the wire with an ordinary Git client so that protocol-facing security
boundaries can be tested before authenticated network services exist.

It does not implement production authentication, TLS, rate limiting, or the
full Phase 4 intake/quarantine architecture. The view name remains the
principal identity.

## Goal

Serve every existing view repository over smart HTTP protocol v0/v1 with a
shared-secret token per view, such that:

- ordinary Git clients clone, fetch, and push with no wrapper commands;
- every request is authorized before any Git process is spawned;
- the existing hook-driven receive pipeline runs unchanged;
- failed or rejected requests leave forge state byte-identical;
- hidden objects are unreachable by ref, by direct OID want, and by
  construction because view repositories never contain them.

## Non-Goals

- TLS termination, certificates, and production secret storage;
- principals, teams, roles, or descriptor binding (spec 005 territory);
- dumb HTTP protocol, static file serving, or repository browsing;
- chunked streaming, compression negotiation beyond request gzip decoding;
- per-IP rate limiting, quotas, or audit logging;
- changing receive validation, projection, outbox, or worker behavior.

## Transport Surface

Only these routes exist. The route grammar is matched exactly against the
decoded request path before any filesystem access:

```text
GET  /<view>.git/info/refs?service=git-upload-pack
GET  /<view>.git/info/refs?service=git-receive-pack
POST /<view>.git/git-upload-pack
POST /<view>.git/git-receive-pack
```

- `<view>` uses the existing view name grammar
  `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`; exactly one trailing `.git` suffix is
  stripped to obtain the candidate view name.
- Any other path, extra segments, a missing or unknown `service` value,
  `service=dumb`, or a trailing slash is `404 Not Found`.
- `info/refs` accepts only `GET`; the two command endpoints accept only
  `POST`. Wrong methods return `405 Method Not Allowed`.
- Requests for a view that does not exist and requests presenting a valid
  token minted for a different view both return `404` with an identical
  generic body, so view existence is not enumerable.
- Requests without credentials or with an unknown token return `401` with a
  `WWW-Authenticate: Basic realm="bastion"` header.

## Authentication Model

Tokens live in `root/tokens.json`, outside `state.json` and outside the
versioned invariant-checker surface:

```json
{"q8Jv...": "alice", "z0Lm...": "bob"}
```

- `forge.py add-token ROOT VIEW` generates a 32-byte urlsafe random token,
  stores the mapping, and prints the token exactly once.
- Token operations take the exclusive `state.lock` but never modify
  `state.json`, refs, or repositories.
- A token authorizes exactly one view for both upload and receive.
- Both `Authorization: Basic` (username ignored, password is the token) and
  `Authorization: Bearer <token>` are accepted.
- The server resolves the token to a view name and compares it to the routed
  view name. Authorization happens before spawning any Git process.

## Request Handling

The server is a stdlib `ThreadingHTTPServer` fronting short-lived Git child
processes. No CGI, no `git http-backend`.

1. Parse and match the route grammar; reject early on mismatch.
2. Authenticate; resolve token to a view; reject on mismatch or unknown view.
3. Verify the view repository directory exists on disk.
4. Decode `Content-Encoding: gzip` request bodies when present.
5. Spawn one process:

```text
git --git-dir <root>/views/<view>.git upload-pack  --stateless-rpc [--advertise-refs]
git --git-dir <root>/views/<view>.git receive-pack --stateless-rpc [--advertise-refs]
```

6. Pipe the request body to stdin; buffer stdout as the response body.
7. Pass the `Git-Protocol` request header value through as `GIT_PROTOCOL`
   verbatim so protocol v2 negotiation works for upload-pack.

Advertisement responses prepend the pkt-line service header and flush-pkt and
use the upstream content types and cache headers:

```text
Content-Type: application/x-git-upload-pack-advertisement
Content-Type: application/x-git-receive-pack-advertisement
Content-Type: application/x-git-upload-pack-result
Content-Type: application/x-git-receive-pack-result
Cache-Control: no-cache, max-age=0, must-revalidate
```

Child processes inherit a cleaned environment (existing `clean_env()`) plus
the hook-visible process environment. Receive hooks therefore run exactly as
they do for local pushes: synchronous validation, canonical reconciliation,
and post-receive job draining.

A child process that exits nonzero produces a `500` response whose body is
the child's stderr, truncated to 4096 bytes. Malformed request encodings
produce `400`. Every error response body is a short fixed string; no forge
paths, state, or object IDs appear in error bodies except receive-pack
stderr, which is already client-safe hook output.

## CLI Contract

```text
python3 forge.py add-token ROOT VIEW
python3 forge.py serve ROOT [--host HOST] [--port PORT]
```

- `add-token` rejects unknown views and prints only the token on success.
- `serve` binds `127.0.0.1:8000` by default, refuses to start when
  `state.json` cannot be loaded, and prints one startup line to stderr.
- Tests use an in-process helper that constructs the server on an ephemeral
  port instead of the CLI.

## Security Posture

This endpoint becomes the modeled Git boundary for the following claims,
which the test suite must demonstrate rather than assume:

1. No unauthenticated byte reaches a Git process.
2. A token grants access to exactly its own view and nothing else.
3. View existence is not distinguishable between unknown views and
   unauthorized views.
4. Hidden canonical objects are unfetchable: they are absent from view
   object stores, so ref-based wants, direct OID wants, and protocol v2
   fetches all fail inside Git itself.
5. Dumb protocol and non-Git surfaces expose nothing.
6. Path traversal and malformed routes never touch the filesystem.
7. Rejected pushes and failed requests leave `state.json`, refs, mappings,
   jobs, and receiver markers byte-identical, reusing the established
   rejection-immutability method.
8. Push over HTTP exercises the same prepare/CAS/publish barriers as local
   pushes; there is no second code path around validation.

Remaining open risks stay open and documented: no TLS, shared tokens are not
identities, no resource limits, buffered bodies load packs into memory, and
error timing is not uniform.

## Deliverables

- `src/http_server.py`: pure route/auth/body helpers plus the server class
  and a `make_server(root, host, port)` test entry point.
- `forge.py`: `add-token` and `serve` commands.
- `tests/test_http_server.py`: end-to-end clone/fetch/push over loopback HTTP
  with real Git clients, fanout verification, and protocol v2 fetch.
- `tests/test_http_security.py`: the adversarial matrix below.

## Required Test Matrix

### End-to-end behavior

- Clone a view over HTTP with Basic and Bearer credentials.
- Push a linear change over HTTP; verify the source ref moved, the canonical
  ref advanced through reconciliation, and a secondary view received its
  synthetic representation after post-receive draining.
- Fetch over HTTP after another view pushed; verify fast-forward adoption.
- Protocol v2 fetch (`GIT_PROTOCOL=version=2`) succeeds.
- Shallow fetch (`--depth 1`) succeeds and stays within projected history.

### Authorization boundaries

- Missing, malformed, empty, and unknown credentials return 401 with
  `WWW-Authenticate` and never spawn Git.
- Valid token against another view returns 404 byte-identical to unknown
  view, including for `info/refs`, upload, and receive endpoints.
- A bob token cannot read alice's advertisement, objects, or refs.
- Revoking a token (removing it from `tokens.json`) takes effect immediately.

### Protocol and route hardening

- Dumb-protocol probes (`GET /<view>.git/HEAD`, `/objects/..`,
  `/info/refs` without service) return 404.
- Wrong methods return 405.
- Encoded traversal (`/%2e%2e/`, `%00`, absolute paths, nested suffixes like
  `/views/alice.git.git/info/refs`) never escape the route grammar.
- Oversized and truncated pkt-line garbage posted to command endpoints fails
  without corrupting state.

### State immutability

- Every rejected HTTP operation leaves `state.json` bytes, all refs, token
  file bytes, and receiver markers unchanged, checked with the existing
  snapshot-comparison approach.
- A push rejected by pre-receive over HTTP returns a nonzero git exit and
  the client keeps its original remote state.

## Acceptance Criteria

- [ ] All four routes work with unmodified Git clients over loopback HTTP.
- [ ] No route spawns a Git process before authorization succeeds.
- [ ] Cross-view access, enumeration, traversal, and dumb-protocol probes
      fail exactly as specified.
- [ ] Hidden-object wants fail inside Git with no server-side special case.
- [ ] Rejected operations are byte-immutability verified.
- [ ] Full existing suite still passes; new modules carry focused unit tests
      for route parsing, credential parsing, and token management.
