import os
import time
import unittest
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM

from neurogrid_node.model import Verifier, checkpoint, configure_cpu, unpack


class InferenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configure_cpu(2)
        cls.path = Path(os.environ["NEUROGRID_MODEL"]) if "NEUROGRID_MODEL" in os.environ else checkpoint()
        cls.verifier = Verifier(cls.path)
        cls.full = AutoModelForCausalLM.from_pretrained(
            cls.path, local_files_only=True, torch_dtype=torch.float32, attn_implementation="eager",
        ).eval()

    @torch.inference_mode()
    def test_greedy_tokens_match_local_inference(self):
        for prompt in ("What is 2 + 2?", "Give one reason bees matter.", "Name a European capital."):
            prepare = self.verifier.process({"kind": "prepare", "prompt": prompt, "maxTokens": 8})
            tokens = prepare["tokens"]
            started = time.perf_counter()
            output = []
            for _ in range(8):
                sequence = tokens + output
                payload = {"tokens": sequence}
                for partition in self.verifier.partitions:
                    payload = partition.run(payload, len(sequence))
                expected = int(self.full(torch.tensor([sequence]), use_cache=False).logits[0, -1].argmax())
                self.assertEqual(payload["token"], expected)
                output.append(expected)
            print(f"\n{prompt!r} 8 matched tokens in {time.perf_counter() - started:.2f}s")

    def test_verifier_rejects_invalid_activation(self):
        payload = {"tokens": [1, 42]}
        work = {"kind": "verify", "input": payload, "stage": 0, "length": 2}
        expected = self.verifier.partitions[0].run(payload, 2)
        self.assertTrue(self.verifier.process({**work, "candidate": expected})["valid"])
        wrong = { "tensor": "A" * len(expected["tensor"]) }
        self.assertFalse(self.verifier.process({**work, "candidate": wrong})["valid"])
        with self.assertRaises(ValueError):
            unpack("invalid", 2)
