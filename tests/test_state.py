from __future__ import annotations

import os
import unittest
import tempfile
from pathlib import Path

from state import (
    apply_transaction_state,
    process_start_time,
    receiver_active,
    recover,
    save_state,
    write_receiver_marker,
)


class TransactionStateTest(unittest.TestCase):
    def test_apply_is_idempotent(self) -> None:
        state = {
            "views": {
                "alice": {
                    "canonical_frontier": "old-canonical",
                    "projected_tip": "old-projected",
                    "anchors": {},
                }
            },
            "changes": {},
        }
        transaction = {
            "view": "alice",
            "canonical_new": "new-canonical",
            "projected_new": "new-projected",
            "mappings": [
                {
                    "submitted": "submitted",
                    "canonical": "new-canonical",
                    "change_id": "Ichange",
                }
            ],
        }

        apply_transaction_state(state, transaction)
        apply_transaction_state(state, transaction)

        self.assertEqual(
            "new-canonical", state["views"]["alice"]["canonical_frontier"]
        )
        self.assertEqual("new-projected", state["views"]["alice"]["projected_tip"])
        self.assertEqual(
            {"canonical": "new-canonical", "kind": "submitted"},
            state["views"]["alice"]["anchors"]["submitted"],
        )
        self.assertEqual(1, len(state["changes"]["Ichange"]["revisions"]))

    def test_mapping_without_change_id_still_creates_anchor(self) -> None:
        state = {
            "views": {
                "alice": {
                    "canonical_frontier": "old",
                    "projected_tip": "old",
                    "anchors": {},
                }
            },
            "changes": {},
        }
        transaction = {
            "view": "alice",
            "canonical_new": "canonical",
            "projected_new": "submitted",
            "mappings": [
                {
                    "submitted": "submitted",
                    "canonical": "canonical",
                    "change_id": None,
                }
            ],
        }
        apply_transaction_state(state, transaction)
        self.assertIn("submitted", state["views"]["alice"]["anchors"])
        self.assertEqual({}, state["changes"])

    def test_older_transaction_cannot_regress_newer_view_state(self) -> None:
        state = {
            "views": {
                "alice": {
                    "canonical_frontier": "canonical-2",
                    "projected_tip": "projected-2",
                    "anchors": {},
                }
            },
            "changes": {},
        }
        older = {
            "view": "alice",
            "canonical_frontier_old": "canonical-0",
            "canonical_old": "canonical-0",
            "canonical_new": "canonical-1",
            "projected_old": "projected-0",
            "projected_new": "projected-1",
            "mappings": [],
        }
        apply_transaction_state(state, older)
        self.assertEqual(
            "canonical-2", state["views"]["alice"]["canonical_frontier"]
        )
        self.assertEqual("projected-2", state["views"]["alice"]["projected_tip"])

    def test_failed_canonical_cas_aborts_prepared_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "canonical.git").mkdir()
            (root / "views" / "alice.git").mkdir(parents=True)
            state = {
                "views": {},
                "changes": {},
                "transactions": {
                    "tx": {
                        "id": "tx",
                        "view": "alice",
                        "canonical_ref": "refs/heads/main",
                        "projected_ref": "refs/heads/main",
                        "canonical_old": "1" * 40,
                        "canonical_new": "2" * 40,
                        "projected_old": "3" * 40,
                        "projected_new": "4" * 40,
                        "status": "prepared",
                        "mappings": [],
                        "sequence": 1,
                    }
                },
            }
            # The Git reads fail to resolve and return ZERO_OID, representing a CAS miss.
            save_state(root, state)
            recover(root, state)
            self.assertEqual("aborted", state["transactions"]["tx"]["status"])

    def test_receiver_marker_checks_process_start_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "receivers").mkdir()
            write_receiver_marker(root, "active", os.getpid())
            self.assertTrue(receiver_active(root, "active"))
            (root / "receivers" / "stale").write_text(
                f"{os.getpid()} not-{process_start_time(os.getpid())}"
            )
            self.assertFalse(receiver_active(root, "stale"))


if __name__ == "__main__":
    unittest.main()
