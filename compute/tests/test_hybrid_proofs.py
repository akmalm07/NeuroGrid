"""Exercise the Q18 bridge and replay real GKR/Halo2 proof bundles."""

from pathlib import Path
from hashlib import sha256
import os
import shutil
import struct
from tempfile import TemporaryDirectory
import unittest

from neurogrid.proofs.hybrid_projection import gkr, verify
from neurogrid.proofs.rescale import FIELD, SCALE, bridge_claim, felt, run
from neurogrid.proofs.hybrid_response import verify_response
from neurogrid.serialization import canonical, read_json


class Q18BridgeTest(unittest.TestCase):
    def test_signed_rounding_ties(self):
        half = SCALE // 2
        raw = [-SCALE - half, -half, 1 - half, 0, half - 1, half, SCALE + half]
        rounded = [-2, -1, 0, 0, 0, 1, 2]
        claim = bridge_claim(raw, rounded, bytes(32), 8)
        self.assertEqual(claim["inputs"][1], [felt(value) for value in [*rounded, 0]])
        self.assertEqual(claim["inputs"][2][:3], [felt(-1)] * 3)
        self.assertEqual(claim["inputs"][3][:7], list(map(felt, [0, 0, SCALE - 1, half, SCALE - 1, 0, 0])))

    def test_out_of_bound_accumulator_rejected(self):
        with self.assertRaisesRegex(ValueError, "bounds"):
            bridge_claim([1 << 52], [0], bytes(32), 8)


@unittest.skipUnless(os.environ.get("NEUROGRID_HYBRID_FIXTURES"),
                     "Set NEUROGRID_HYBRID_FIXTURES to the real Granite hybrid fixture directory")
class HybridProofTest(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ["NEUROGRID_HYBRID_FIXTURES"])
        self.case, self.alternate = root / "ref-tile", root / "ref-tile-alt"
        self.public = root / "bridge8" / "public"
        self.binary = Path(os.environ.get("NEUROGRID_GKR_BIN", root / "tensor.wasm"))
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.job = Path(scratch.name) / "bundle"
        shutil.copytree(self.case, self.job)
        self.expected = self.case / "scope.json"

    def verify(self):
        return verify(self.job, self.expected, self.public, self.binary)

    def test_real_granite_tile_and_q18_bridge_verify(self):
        self.assertEqual(self.verify()["outputs"], 8)

    def test_halo2_proves_exact_half_ties_and_rejects_wrong_rounding(self):
        folder = self.job.parent / "rounding"
        folder.mkdir()
        (folder / "scope.json").write_bytes(canonical({"M": 1, "N": 4}))
        half = SCALE // 2
        (folder / "raw.bin").write_bytes(struct.pack("<4q", -half, half, -half + 1, half - 1))
        output = folder / "rounded.bin"
        output.write_bytes(struct.pack("<4q", -1, 1, 0, 0))
        statement = "d" * 64
        run(self.public.parent, folder, statement, prove=True)
        self.assertEqual(run(self.public, folder, statement, prove=False)["outputs"], 4)
        output.write_bytes(struct.pack("<4q", 0, 1, 0, 0))
        with self.assertRaisesRegex(ValueError, "independently supplied claim"):
            run(self.public, folder, statement, prove=False)

    def test_matrix_multiplication_handles_padding_on_all_axes(self):
        folder = self.job.parent / "odd-matrix"
        folder.mkdir()
        a = (1, 0, 2, -1, 1, 0, 2, 1, -1)
        b = (1, -2, 0, 0, 1, 2, 1, 0, -1)
        expected = (3, -2, -2, -1, 3, 2, 1, -3, 3)
        left, right = (struct.pack("<9i", *(value * SCALE for value in lanes)) for lanes in (a, b))
        for name, data in (("left.bin", left), ("right.bin", right),
                           ("raw.bin", struct.pack("<9q", *(value * SCALE**2 for value in expected))),
                           ("rounded.bin", struct.pack("<9q", *(value * SCALE for value in expected)))):
            (folder / name).write_bytes(data)
        scope = read_json(self.expected) | {"M": 3, "K": 3, "N": 3, "Operation": "test-padding",
                                           "Left": sha256(left).hexdigest(), "Right": sha256(right).hexdigest()}
        (folder / "scope.json").write_bytes(canonical(scope))
        statement = gkr(folder, self.binary, "prove")
        run(self.public.parent, folder, statement, prove=True)
        self.assertEqual(verify(folder, folder / "scope.json", self.public, self.binary)["outputs"], 9)

    def test_changed_rounded_opening_fails_cross_protocol_binding(self):
        path = self.job / "rounded.bin"
        opening = bytearray(path.read_bytes())
        opening[0] ^= 1
        path.write_bytes(opening)
        with self.assertRaisesRegex(ValueError, "independently supplied claim"):
            self.verify()

    def test_independently_valid_bridge_cannot_be_spliced_between_requests(self):
        other = self.alternate
        self.assertEqual((other / "rounded.bin").read_bytes(), (self.case / "rounded.bin").read_bytes())
        self.assertEqual(verify(other, other / "scope.json", self.public, self.binary)["outputs"], 8)
        shutil.copyfile(other / "bridge" / "0.json", self.job / "bridge" / "0.json")
        with self.assertRaisesRegex(ValueError, "independently supplied claim"):
            self.verify()

    def test_different_scope_or_weight_opening_is_rejected(self):
        path = self.job / "scope.json"
        original = read_json(path)
        for field, value in (("Model", "another-model"), ("Layer", 1), ("Position", 7),
                             ("Prompt", "a" * 64), ("KVBefore", "b" * 64), ("KVAfter", "c" * 64)):
            with self.subTest(field=field):
                path.write_bytes(canonical(original | {field: value}))
                with self.assertRaisesRegex(ValueError, "independent statement"):
                    self.verify()
                # Even a verifier assigned to the changed scope must reject the old proof.
                with self.assertRaises(ValueError):
                    verify(self.job, path, self.public, self.binary)
        shutil.copyfile(self.expected, path)
        path = self.job / "right.bin"
        changed = bytearray(path.read_bytes())
        changed[17] ^= 1
        path.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "tensor opening"):
            self.verify()

    def test_sum_preserving_product_forgery_and_transcript_mutation_fail(self):
        path = self.job / "gkr.proof"
        original = path.read_bytes()
        for changes in (((0, 1), (32, -1)), ((len(original) - 32, 1),)):
            with self.subTest(changes=changes):
                tampered = bytearray(original)
                for start, change in changes:
                    end = start + 32
                    lane = int.from_bytes(tampered[start:end], "big")
                    tampered[start:end] = ((lane + change) % FIELD).to_bytes(32, "big")
                path.write_bytes(tampered)
                with self.assertRaises(ValueError):
                    self.verify()


@unittest.skipUnless(os.environ.get("NEUROGRID_HYBRID_RESPONSES"),
                     "Set NEUROGRID_HYBRID_RESPONSES to independently proved small-model responses")
class HybridResponseTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ["NEUROGRID_HYBRID_RESPONSES"])
        self.inputs = self.root / "small-isolated"

    def verify(self, job: Path, replies: int):
        request = self.inputs / ("request.json" if replies == 1 else "request-b.json")
        binary = Path(os.environ.get("NEUROGRID_GKR_BIN", self.inputs / "tensor.wasm"))
        return verify_response(self.inputs / "model-public", request, job, binary,
                               self.inputs / "bridge-public", replies)

    def test_one_and_two_token_traces_verify(self):
        for job, count, tokens, projections in (("small-one", 1, [1], 17), ("small-two", 2, [2, 3], 26)):
            with self.subTest(count=count):
                result = self.verify(self.root / job, count)
                self.assertEqual(result["reply"], tokens)
                self.assertEqual(result["hybrid_projections"], projections)

    def test_matrix_proof_cannot_be_spliced_into_the_other_trace(self):
        with TemporaryDirectory() as scratch:
            job = Path(scratch) / "trace"
            shutil.copytree(self.root / "small-two", job)
            projection = Path("projections") / "p0-q"
            shutil.copytree(self.root / "small-one" / projection, job / projection, dirs_exist_ok=True)
            with self.assertRaisesRegex(ValueError, "verified edge|independent statement"):
                self.verify(job, 2)


if __name__ == "__main__":
    unittest.main()
