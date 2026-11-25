import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

try:
    from keyring.errors import NoKeyringError
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None
else:
    from neurogrid_node.desktop import ComputeLimitsDialog, NodeWindow

from neurogrid_node.profile import ProfileStore


@unittest.skipIf(QApplication is None, "Install the desktop extra to exercise the Qt settings entry points")
class DesktopProfileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = ProfileStore(Path(temp.name) / "profile.json")
        keyring = patch("neurogrid_node.desktop.keyring.get_password", side_effect=NoKeyringError())
        keyring.start()
        self.addCleanup(keyring.stop)

    def window(self):
        window = NodeWindow(False, self.store)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        return window

    def test_save_cancel_and_validate_before_start_without_keyring(self):
        window = self.window()
        self.assertFalse(window.key.text())
        dialog = ComputeLimitsDialog(window)
        dialog.duty.setValue(35)
        dialog.reject()
        self.assertEqual(self.store.read().duty, 75)
        dialog = ComputeLimitsDialog(window)
        dialog.site.setText("http://non-local.example")
        dialog.threads.setValue(1)
        dialog.duty.setValue(35)
        dialog.save()
        self.assertIn("HTTPS site", dialog.error.text())
        self.assertEqual(self.store.read().duty, 75)
        dialog.site.setText("http://127.0.0.1:5181/")
        dialog.save()
        self.assertIsNone(window.thread)
        reopened = self.window()
        self.assertIn("1 CPU thread(s), 35%", reopened.saved_limits.text())
        self.assertEqual(reopened.profile.site, "http://127.0.0.1:5181")

    def test_settings_menu_during_compute_keeps_effective_limits_until_restart(self):
        window = self.window()
        self.store.update({"threads": 1, "duty": 60})
        window.key.setText("test-only")
        started = Event()

        def run(_client, stop, _store, threads, duty, report):
            self.assertEqual((threads, duty), (1, 60))
            started.set()
            stop.wait(5)

        with (
            patch("neurogrid_node.desktop.run_configured_node", side_effect=run),
            patch("neurogrid_node.desktop.keyring.set_password", side_effect=NoKeyringError()),
        ):
            window.toggle_compute()
            try:
                self.assertTrue(started.wait(3))
                observed = []

                def edit():
                    dialog = window.findChild(ComputeLimitsDialog)
                    observed.append(dialog.effective.text())
                    dialog.duty.setValue(25)
                    dialog.save()

                QTimer.singleShot(0, edit)
                if window.tray:
                    window.close()
                    self.assertFalse(window.isVisible())
                    next(action for action in window.tray.contextMenu().actions()
                         if action.text() == "Compute limits").trigger()
                    self.assertTrue(window.isVisible())
                else:
                    window.open_limits()
                self.assertEqual(observed, ["1 CPU thread(s), 60% approximate duty cycle"])
                self.assertIn("Saved: 1 CPU thread(s), 25%", window.saved_limits.text())
                self.assertIn("Effective: 1 CPU thread(s), 60%", window.saved_limits.text())
            finally:
                window.toggle_compute()
                self.assertTrue(window.thread.wait(6000))
                self.app.processEvents()
            self.assertTrue(self.store.read().paused)
