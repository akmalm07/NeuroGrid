"""NeuroGrid CPU inference and desktop volunteer client."""

import json
from pathlib import Path

MANIFEST = json.loads(Path(__file__).with_name("granite.json").read_text(encoding="utf-8"))
MODEL = MANIFEST["model"]
REVISION = MANIFEST["revision"]
RUNTIME = MANIFEST["runtime"]
SHARD_COUNT = MANIFEST["shards"]
LAST_SHARD = SHARD_COUNT - 1
CONTEXT = MANIFEST["context"]
WINDOW = MANIFEST["window"]
