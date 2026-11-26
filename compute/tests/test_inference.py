import os
import json
import time
import unittest
from contextlib import ExitStack
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, GraniteConfig, GraniteForCausalLM
from transformers.models.granite.modeling_granite import GraniteRotaryEmbedding

from neurogrid.models.manifest import WINDOW
from neurogrid.models.granite import (
    CacheMiss, Granite, checkpoint, configure_cpu, matches_activation, tokenize_prompt, unpack, verified_file,
    weight_reader,
)


@unittest.skipUnless(os.environ.get("NEUROGRID_MODEL") or os.environ.get("NEUROGRID_DOWNLOAD_TEST_MODEL"),
                     "Set NEUROGRID_MODEL to the pinned Granite checkpoint")
class InferenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configure_cpu(2)
        cls.path = Path(os.environ["NEUROGRID_MODEL"]) if "NEUROGRID_MODEL" in os.environ else checkpoint()
        cls.model = Granite(cls.path)

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
            tokens = tokenize_prompt(self.model.tokenizer, prompt)
            self.assertLessEqual(len(tokens), WINDOW)
            started = time.perf_counter()
            reply = self.model.generate(prompt, max_tokens=3)
            self.assertEqual(reply["execution"], "bf16-reference")
            self.assertEqual(len(reply["tokens"]), 3)
            state = None
            for token in reply["tokens"]:
                result = full(torch.tensor([tokens]), use_cache=True, past_key_values=state)
                state = result.past_key_values
                expected = int(result.logits[0, -1].argmax())
                self.assertEqual(token, expected)
                tokens = [expected]
            print(f"\n{prompt!r} three matching BF16 tokens in {time.perf_counter() - started:.2f}s")

    def test_verifier_rejects_invalid_activation_and_missing_kv_state(self):
        payload = {"tokens": [1, 42]}
        expected = self.model.partitions[0].run(payload, 2)["tensor"]
        self.assertTrue(matches_activation(expected, expected, 2))
        self.assertFalse(matches_activation("A" * len(expected), expected, 2))
        with self.assertRaises(ValueError):
            unpack("invalid", 2)
        with self.assertRaises(CacheMiss):
            self.model.partitions[0].run({"tokens": [42]}, 1, position=2, job="missing")

    def test_pinned_prompt_and_generation_bounds(self):
        phrase = "What is the capital of France?"
        tokens = tokenize_prompt(self.model.tokenizer, phrase)
        self.assertIn(phrase, self.model.tokenizer.decode(tokens))
        for invalid in ("", " " * 33_000):
            with self.assertRaises(ValueError):
                tokenize_prompt(self.model.tokenizer, invalid)
        with self.assertRaisesRegex(ValueError, "available context"):
            self.model.generate(phrase, max_tokens=0)
