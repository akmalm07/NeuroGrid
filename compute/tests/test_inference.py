import os
import json
import time
import unittest
from contextlib import ExitStack
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, GraniteConfig, GraniteForCausalLM
from transformers.models.granite.modeling_granite import GraniteRotaryEmbedding

from neurogrid_node import WINDOW
from neurogrid_node.model import (
    CacheMiss, Verifier, checkpoint, configure_cpu, prepare_context, unpack, verified_file, weight_reader,
)


@unittest.skipUnless(os.environ.get("NEUROGRID_MODEL") or os.environ.get("NEUROGRID_DOWNLOAD_TEST_MODEL"),
                     "Set NEUROGRID_MODEL to the pinned Granite checkpoint")
class InferenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configure_cpu(2)
        cls.path = Path(os.environ["NEUROGRID_MODEL"]) if "NEUROGRID_MODEL" in os.environ else checkpoint()
        cls.verifier = Verifier(cls.path)

    @torch.inference_mode()
    def test_greedy_tokens_match_local_inference(self):
        if os.name == "nt":
            config = GraniteConfig.from_json_file(str(verified_file(self.path, "config.json")))
            config._attn_implementation = "sdpa"
            with torch.device("meta"):
                full = GraniteForCausalLM(config)
            full.model.rotary_emb = GraniteRotaryEmbedding(config)
            index = json.loads(verified_file(self.path, "model.safetensors.index.json").read_text())["weight_map"]
            with ExitStack() as stack:
                files = {name: weight_reader(verified_file(self.path, name), stack) for name in set(index.values())}
                full.load_state_dict({name: files[index[name]].get_tensor(name) for name in full.state_dict()}, assign=True)
            full.eval().requires_grad_(False)
        else:
            full = AutoModelForCausalLM.from_pretrained(
                self.path, local_files_only=True, dtype=torch.bfloat16, attn_implementation="sdpa",
            ).eval()
        prompts = ("What is the capital of France?", "What is 2 + 2?", "Summarize why pollinators matter.")
        for prompt in prompts:
            tokens = prepare_context(self.verifier.tokenizer, [{"role": "user", "content": prompt}])["tokens"]
            self.assertLessEqual(len(tokens), WINDOW)
            started = time.perf_counter()
            state, position = None, 0
            for _ in range(3):
                inputs = {"tokens": tokens}
                for partition in self.verifier.partitions:
                    inputs = partition.run(inputs, len(tokens), position=position, job="parity")
                result = full(torch.tensor([tokens]), use_cache=True, past_key_values=state)
                state = result.past_key_values
                expected = int(result.logits[0, -1].argmax())
                self.assertEqual(inputs["token"], expected)
                position += len(tokens)
                tokens = [expected]
            print(f"\n{prompt!r} three matching BF16 tokens in {time.perf_counter() - started:.2f}s")

    def test_verifier_rejects_invalid_activation_and_missing_kv_state(self):
        payload = {"tokens": [1, 42]}
        work = {"id": "verification", "kind": "verify", "input": payload, "stage": 0, "length": 2,
                "job": "verification", "epoch": 0, "position": 0, "emit": True}
        expected = self.verifier.partitions[0].run(payload, 2)
        self.assertTrue(self.verifier.process({**work, "candidate": expected})["valid"])
        wrong = {"tensor": "A" * len(expected["tensor"])}
        self.assertFalse(self.verifier.process({**work, "candidate": wrong})["valid"])
        with self.assertRaises(ValueError):
            unpack("invalid", 2)
        with self.assertRaises(CacheMiss):
            self.verifier.partitions[0].run({"tokens": [42]}, 1, position=2, job="missing")

    def test_context_preserves_recent_roles_and_trims_old_pairs(self):
        turns = [
            {"role": "user", "content": "Older" + " very" * 300},
            {"role": "assistant", "content": "Acknowledged the old turn."},
            {"role": "user", "content": "My favorite city is Kyoto."},
            {"role": "assistant", "content": "I will remember Kyoto."},
            {"role": "user", "content": "Which city is my favorite?"},
        ]
        prepared = prepare_context(self.verifier.tokenizer, turns)
        self.assertEqual((prepared["profile"], prepared["trimmed"]), ("quick", 1))
        rendered = self.verifier.tokenizer.decode(prepared["tokens"])
        self.assertIn("I will remember Kyoto", rendered)
        self.assertIn("Which city is my favorite?", rendered)
        self.assertNotIn("Acknowledged the old turn", rendered)
