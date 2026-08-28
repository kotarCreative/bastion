# Git Compatibility Matrix

Bastion treats each projected Git object database as a confidentiality
boundary. This is stricter than hiding refs: Git protocol v2 permits exact OID
wants for objects that are physically present but unadvertised. Compatibility
tests therefore assert that every object in an active view is reachable from
its advertised ref and that known canonical-only commits, trees, and blobs are
absent.

The initial matrix in `tests/test_git_compatibility.py` covers:

| Area | Cases | Security oracle |
| --- | --- | --- |
| Maintenance | commit-graph, multi-pack-index, bitmap repack, `gc --prune=now`, contaminated cruft generation | Exact reachable object closure, unchanged refs/history, strict fsck, no alternates, unchanged repository truth; replacement of a generation containing unreachable objects |
| Smart HTTP | requested protocol versions 0, 1, and 2 | Clone succeeds; direct wants for hidden commit/tree/blob and an unknown OID fail |
| Shallow history | depth-one clone, deepen, unshallow | Expected visible history is recovered without importing hidden objects |
| Post-maintenance receive | authenticated push after repack and GC | Intake alternate remains usable and the next active generation has exact closure |

Requested protocol version 1 currently falls back to Bastion's sanitized v0
path; only a valid version 2 header is forwarded to Git. The matrix records
client compatibility with that behavior, not native protocol v1 support.

## Upstream Inspiration

The edge cases come from Git's own test suite and security documentation:

- `Documentation/transfer-data-leaks.adoc`: hidden refs and namespaces do not
  prevent exact OID or delta-similarity disclosures.
- `t/t5516-fetch-push.sh`: exact OID fetches, including reachable and
  unreachable objects.
- `t/t5537-fetch-shallow.sh`, `t/t5539-fetch-http-shallow.sh`, and
  `t/t5702-protocol-v2.sh`: shallow boundaries, deepen, and unshallow.
- `t/t6500-gc.sh`, `t/t7701-repack-unpack-unreachable.sh`, and
  `t/t7704-repack-cruft.sh`: unreachable objects, cruft packs, repack, and GC.
- `t/t5310-pack-bitmaps.sh`: reused delta bases during bitmap fetches.
- `t/t5318-commit-graph.sh`: stale commit-graph data and object removal.
- `t/t5319-multi-pack-index.sh` and `t/t5326-multi-pack-bitmaps.sh`: stale,
  missing, expired, and alternate-backed packs.
- `t/t5306-pack-nobase.sh` and `t/t5309-pack-delta-cycles.sh`: missing thin-pack
  bases and pathological delta graphs.

## Next Rows

- Permission contraction followed by cruft-pack and GC maintenance across the
  projection-epoch boundary.
- A malicious inbound thin pack whose delta base exists only in canonical
  storage, plus delta cycles and duplicate-base fallback.
- MIDX expiry/repack, missing-pack behavior, stale bitmap cleanup, and
  `GIT_COMMIT_GRAPH_PARANOIA=true`.
- Shallow deepen attempts across a parentless policy epoch, plus
  `--shallow-since` and `--shallow-exclude`.
- Partial clone capability negotiation and fail-closed behavior.
- Equivalent hidden-object and thin-pack cases through the SSH entry point.
- A CI matrix over explicitly supported Git versions.
