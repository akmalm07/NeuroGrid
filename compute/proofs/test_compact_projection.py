"""Replay full-query receipts against independently installed commitments."""

import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from compact_benchmark import RECEIPT, command, verify


@unittest.skipUnless(os.environ.get("NEUROGRID_COMPACT_CASE"),
                     "Set NEUROGRID_COMPACT_CASE to the two full Granite compact fixtures")
class CompactProjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ["NEUROGRID_COMPACT_CASE"])
        cls.binary = Path(os.environ["NEUROGRID_COMPACT_BIN"])
        cls.model = cls.root / "setup-full" / "public"
        cls.request = cls.root / "request-a"
        cls.receipt = cls.root / "job-a" / RECEIPT

    def test_full_projection_without_tensor_files(self):
        checked = verify(self.binary, self.model, self.request, self.receipt)
        self.assertLess(checked["per_request_bytes"], 1 << 20)
        self.assertLess(checked["installed_verifier_data_bytes"], 200_000)
        self.assertEqual(checked["returned_output_bytes"], 0)

    def test_folded_weight_polynomial_opening_is_authenticated(self):
        corrupt = bytearray(self.receipt.read_bytes())
        proof_end = 98 + int.from_bytes(corrupt[96:98], "big")
        product_rows = 64 * 32
        transcript_start = proof_end + product_rows
        fields = int.from_bytes(corrupt[transcript_start:transcript_start + 2], "big")
        weights = transcript_start + 2 + fields * 32 + 64 * 32
        corrupt[weights + 31] ^= 1
        with TemporaryDirectory() as temp:
            receipt = Path(temp) / RECEIPT
            receipt.write_bytes(corrupt)
            with self.assertRaisesRegex(ValueError, "authenticated polynomial opening"):
                verify(self.binary, self.model, self.request, receipt)

    def test_genuine_rescale_proof_cannot_be_spliced_into_another_request(self):
        another = self.root / "job-b" / RECEIPT
        verify(self.binary, self.model, self.root / "request-b", another)
        with self.assertRaisesRegex(ValueError, "batched rescale proof rejected"):
            verify(self.binary, self.model, self.request, another)
        changed, original = bytearray(self.receipt.read_bytes()), another.read_bytes()
        bridge_end = 98 + int.from_bytes(original[96:98], "big")
        changed[98:bridge_end] = original[98:bridge_end]
        with TemporaryDirectory() as temp:
            receipt = Path(temp) / RECEIPT
            receipt.write_bytes(changed)
            with self.assertRaisesRegex(ValueError, "batched rescale proof rejected"):
                verify(self.binary, self.model, self.request, receipt)

    def test_model_commitment_and_request_substitution_are_rejected(self):
        with TemporaryDirectory() as temp:
            alternate = Path(temp)
            model = alternate / "model"
            shutil.copytree(self.model, model)
            points = model / "model.commit"
            changed = bytearray(points.read_bytes())
            changed[5] ^= 1
            points.write_bytes(changed)
            with self.assertRaisesRegex(ValueError, "model commitment"):
                verify(self.binary, model, self.request, self.receipt)

            request = alternate / "request"
            shutil.copytree(self.request, request)
            scope = request / "scope.json"
            claim = json.loads(scope.read_bytes())
            claim["KVAfter"] = "1" * 64
            scope.write_text(json.dumps(claim), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "different request or model"):
                verify(self.binary, self.model, request, self.receipt)

    def test_returned_q18_tensor_opens_the_proved_output(self):
        genuine = self.root / "full-pinned" / "rounded.bin"
        checked = verify(self.binary, self.model, self.request, self.receipt, genuine)
        self.assertEqual(checked["returned_output_bytes"], 4096 * 8)
        changed = bytearray(genuine.read_bytes())
        changed[0] ^= 1
        with TemporaryDirectory() as temp:
            output = Path(temp) / "changed-q18.bin"
            output.write_bytes(changed)
            with self.assertRaisesRegex(ValueError, "proved output commitment"):
                verify(self.binary, self.model, self.request, self.receipt, output)

    def test_checked_installation_pin_is_required_for_fast_key_loading(self):
        with self.assertRaisesRegex(ValueError, "proving key differs from its trusted installation pin"):
            command(self.binary, "compact-prove", self.root / "job-a", key_pin="1" * 64)


if __name__ == "__main__":
    unittest.main()
