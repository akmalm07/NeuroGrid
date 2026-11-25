import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from neurogrid_node.profile import ProfileStore
from neurogrid_node.service import run_service


class ServiceProfileTest(unittest.TestCase):
    def test_persistent_pause_skips_enrollment(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ):
            root = Path(folder)
            profile = root / "profile.json"
            ProfileStore(profile).update({"paused": True})
            messages = []
            result = run_service(profile, root / "absent.key", root / "cache", Event(), messages.append)
            self.assertEqual(result, 0)
            self.assertEqual(messages, ["Paused by the contributor"])

    def test_invalid_service_credential_is_redacted(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ):
            root = Path(folder)
            credential = root / "node.key"
            credential.write_text("invalid-credential-fixture", encoding="ascii")
            messages = []
            result = run_service(root / "profile.json", credential, root / "cache", Event(), messages.append)
            self.assertEqual(result, 78)
            self.assertEqual(messages, ["Cannot read the service profile or enrollment"])
