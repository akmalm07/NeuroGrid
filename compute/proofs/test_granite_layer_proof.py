"""Exercise native setup, proof verification, and rejection in the benchmark harness."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import ezkl
import numpy as np
import onnx
import onnxruntime

from check_granite_decode_backend import benchmark_proof, center_logits


class LayerProofHarnessTest(unittest.TestCase):
    def test_centering_preserves_softmax_across_key_axis(self):
        helper = onnx.helper
        float32 = onnx.TensorProto.FLOAT
        graph = helper.make_graph(
            [helper.make_node("Softmax", ["logits"], ["scores"], axis=-1)], "attention",
            [helper.make_tensor_value_info("logits", float32, [2, 3])],
            [helper.make_tensor_value_info("scores", float32, [2, 3])],
        )
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 18)], ir_version=8)
        values = {"logits": np.array([[25.1, 0.01, -32.5], [-35.5, -32.1, -33.3]], dtype=np.float32)}
        providers = ["CPUExecutionProvider"]
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        expected = onnxruntime.InferenceSession(model.SerializeToString(), options, providers=providers)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "attention.onnx"
            onnx.save(model, path)
            center_logits(path)
            rewritten = onnxruntime.InferenceSession(str(path), options, providers=providers)
            np.testing.assert_allclose(rewritten.run(None, values), expected.run(None, values), rtol=1e-6)

    def test_isolated_halo2_verifier_checks_claim_and_proof(self):
        # Small enough to exercise native setup and tamper checks on a developer machine.
        with TemporaryDirectory() as directory:
            scratch = Path(directory)
            helper = onnx.helper
            float32 = onnx.TensorProto.FLOAT
            graph = helper.make_graph(
                [helper.make_node("Add", ["x", "y"], ["result"])], "benchmark",
                [helper.make_tensor_value_info(name, float32, [1]) for name in ("x", "y")],
                [helper.make_tensor_value_info("result", float32, [1])],
            )
            model = scratch / "test.onnx"
            onnx.save(helper.make_model(graph, opset_imports=[helper.make_opsetid("", 18)], ir_version=8), model)

            sample = scratch / "input.json"
            sample.write_text(json.dumps({"input_data": [[2], [-1.5]]}), encoding="utf-8")
            settings = scratch / "ezkl-settings.json"
            circuit = scratch / "layer0.ezkl"
            srs = scratch / "test.srs"
            options = ezkl.PyRunArgs()
            options.check_mode = "safe"
            options.input_scale = options.param_scale = 10
            options.logrows = 11
            options.lookup_range = (-20, 20)
            options.decomp_base = 128
            self.assertTrue(ezkl.gen_settings(str(model), str(settings), options))
            self.assertTrue(ezkl.compile_circuit(str(model), str(circuit), str(settings)))
            ezkl.gen_srs(str(srs), options.logrows)
            witness = ezkl.gen_witness(str(sample), str(circuit), str(scratch / "witness.json"))

            benchmark_proof(scratch, srs, witness)
            witness["inputs"][0][0] = "0" * 64
            with self.assertRaisesRegex(ValueError, "independently quantized benchmark input"):
                benchmark_proof(scratch, srs, witness)
