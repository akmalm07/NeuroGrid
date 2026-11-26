"""Tests for the segmented wire protocol and optional replay of genuine proof bundles."""

from copy import deepcopy
from base64 import b64encode
from pathlib import Path
import os
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import ezkl
import onnx

from segmented_benchmark import paths, quantized_constants
from segmented_model import model_plan, projection
from segmented_receipt import commit, context_fields, field, read_json, verify_bundle, write_json


class SegmentPlanTest(unittest.TestCase):
    def test_row_tiling_keeps_full_fan_in_and_fixed_input_order(self):
        graph = projection(8, 4096).graph
        self.assertEqual([node.op_type for node in graph.node], ["MatMul", "Identity"])
        self.assertEqual([v.name for v in graph.input], ["x0", "weights", "context"])
        self.assertEqual([dim.dim_value for dim in graph.input[1].type.tensor_type.shape.dim], [4096, 8])
        self.assertEqual(graph.output[0].type.tensor_type.shape.dim[-1].dim_value, 8)
        onnx.checker.check_graph(graph)

    def test_complete_plan_routes_cache_and_generated_token(self):
        constants, plan, reply_edges = model_plan()
        known = set(constants) | {"prompt0", "prompt1"}
        for segment in plan:
            self.assertTrue(set(segment.inputs) <= known)
            self.assertFalse(set(segment.outputs) & known)
            known.update(segment.outputs)
        self.assertTrue(set(reply_edges) <= known)
        self.assertIn(reply_edges[0], next(step.inputs for step in plan if step.name == "p2-embed"))
        self.assertIn("p1-keys", next(step.inputs for step in plan if step.name == "p2-attention"))
        self.assertIn("p1-values", next(step.inputs for step in plan if step.name == "p2-attention"))

    def test_context_changes_when_layer_position_or_operation_changes(self):
        request = {"nonce": "a" * 64, "prompt": [0, 1], "manifest": "b" * 64}
        segment = {"name": "q", "layer": 0, "position": 0, "inputs": ["x", "weight"], "operation": "linear"}
        anchor = context_fields(request, segment)
        for change in ({"layer": 1}, {"position": 1}, {"operation": "attention"}, {"inputs": ["weight", "x"]}):
            self.assertNotEqual(anchor, context_fields(request, segment | change))


@unittest.skipUnless(os.environ.get("NEUROGRID_SEGMENTED_CASE"),
                     "Set NEUROGRID_SEGMENTED_CASE to an independently verified benchmark directory")
class SegmentedProofTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ["NEUROGRID_SEGMENTED_CASE"])
        self.public = self.root / "public"
        self.request = read_json(self.root / "request.json")
        self.bundle = read_json(self.root / "bundle.json")
        self.other = read_json(self.root / "bundle-b.json") if (self.root / "bundle-b.json").exists() else None

    def test_complete_inference_verifies(self):
        report = verify_bundle(self.public, self.request, self.bundle)
        self.assertEqual(report["segments"], 54)
        self.assertEqual(report["reply"], [1, 1])

    def test_missing_segment_or_wrong_reply_is_rejected(self):
        missing = self.bundle | {"segments": self.bundle["segments"][:-1]}
        with self.assertRaisesRegex(ValueError, "exactly one proof"):
            verify_bundle(self.public, self.request, missing)
        wrong = self.bundle | {"reply": [0, self.bundle["reply"][1]]}
        with self.assertRaisesRegex(ValueError, "Reply tokens"):
            verify_bundle(self.public, self.request, wrong)

    def test_relabeling_request_or_splicing_another_valid_segment_fails(self):
        nonce = ("a" if not self.request["nonce"].startswith("a") else "b") * 64
        with self.assertRaisesRegex(ValueError, "rejected at p0-embed"):
            verify_bundle(self.public, self.request | {"nonce": nonce}, self.bundle | {"request": nonce})
        tampered = deepcopy(self.bundle)
        q, kv = (next(item for item in tampered["segments"] if item["name"] == name) for name in ("p0-q", "p0-kv"))
        q["proof"], q["outputs"] = kv["proof"], kv["outputs"]
        with self.assertRaisesRegex(ValueError, "rejected at p0-q"):
            verify_bundle(self.public, self.request, tampered)
        if self.other:
            tampered = deepcopy(self.bundle)
            tampered["segments"][0] = self.other["segments"][0]
            with self.assertRaisesRegex(ValueError, "rejected at p0-embed"):
                verify_bundle(self.public, self.request, tampered)

    def test_cache_commitment_substitution_fails(self):
        tampered = deepcopy(self.bundle)
        attention = next(item for item in tampered["segments"] if item["name"] == "p0-attention")
        attention["outputs"][1] = commit([field(0)] * 6)
        with self.assertRaisesRegex(ValueError, "rejected at p0-attention"):
            verify_bundle(self.public, self.request, tampered)

    def test_valid_proof_with_substituted_weights_fails(self):
        segment = read_json(self.public / "manifest.json")["segments"][0]
        _, circuit, _, pk = paths(self.root, segment["operation"])
        weights = quantized_constants()["embedding"].copy()
        weights[0] = field(0)
        inputs = [[field(self.request["prompt"][0])], weights, context_fields(self.request, segment)]
        with TemporaryDirectory() as scratch, patch.dict(os.environ, {"EZKL_REPO_PATH": str(self.root)}):
            data, witness = Path(scratch) / "input.json", Path(scratch) / "witness.json"
            write_json(data, {"input_data": inputs})
            evaluated = ezkl.gen_witness(str(data), str(circuit), str(witness))
            proof = ezkl.prove(str(witness), str(circuit), str(pk), None, str(self.public / "params.srs"))
        tampered = deepcopy(self.bundle)
        tampered["segments"][0]["outputs"] = evaluated["processed_outputs"]["poseidon_hash"][:-1]
        tampered["segments"][0]["proof"] = b64encode(bytes.fromhex(proof["proof"][2:])).decode()
        with self.assertRaisesRegex(ValueError, "rejected at p0-embed"):
            verify_bundle(self.public, self.request, tampered)
