"""PySide6 controller for a single volunteer partition."""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from threading import Event

import keyring
from keyring.errors import KeyringError
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QPushButton, QSpinBox, QStyle, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)

from .client import GridClient, GridError, run_configured_node
from .profile import ContributorProfile, MAX_THREADS, ProfileStore, WorkerStatus

SERVICE = "NeuroGrid Node"
MODULE = "neurogrid_node.desktop"


def desktop_entry_arg(value: str) -> str:
    escaped = value.replace("%", "%%")
    for special in ('\\', '"', '`', '$'):
        escaped = escaped.replace(special, '\\' + special)
    return '"' + escaped.replace('\\', '\\\\') + '"'


def set_autostart(enabled: bool) -> None:
    # Prefer pythonw on Windows so sign-in doesn't open a console window.
    executable = Path(sys.executable)
    if sys.platform == "win32":
        import winreg
        gui = executable.with_name("pythonw.exe")
        args = [str(gui if gui.exists() else executable), "-m", MODULE, "--autostart"]
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            if enabled:
                winreg.SetValueEx(key, "NeuroGridNode", 0, winreg.REG_SZ, subprocess.list2cmdline(args))
            else:
                try:
                    winreg.DeleteValue(key, "NeuroGridNode")
                except FileNotFoundError:
                    pass
        return
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    location = config / "autostart" / "neurogrid-node.desktop"
    if enabled:
        location.parent.mkdir(exist_ok=True, parents=True)
        command = " ".join([desktop_entry_arg(str(executable)), "-m", MODULE, "--autostart"])
        location.write_text(f"[Desktop Entry]\nType=Application\nName=NeuroGrid Node\nExec={command}\n", encoding="utf-8")
    else:
        location.unlink(missing_ok=True)


class ComputeThread(QThread):
    status = Signal(str)

    def __init__(self, client: GridClient, profile: ContributorProfile, store: ProfileStore):
        super().__init__()
        self.client = client
        self.profile = profile
        self.store = store
        self.stop = Event()

    def run(self):
        try:
            with self.store.worker():
                run_configured_node(self.client, self.stop, self.store,
                                    self.profile.threads, self.profile.duty, report=self.status.emit)
        except GridError as exc:
            self.status.emit(
                "Key rejected · create a new key in Downloads" if exc.status == 401
                else f"Coordinator rejected the connection · {exc}",
            )
        except ValueError as exc:
            self.status.emit(str(exc))
        except (OSError, RuntimeError):
            self.status.emit("Could not load or connect. Check the site, network and model cache, then retry.")


def describe(profile: ContributorProfile | WorkerStatus) -> str:
    return f"{profile.threads} CPU thread(s), {profile.duty}% approximate duty cycle"


class ComputeLimitsDialog(QDialog):
    def __init__(self, window: "NodeWindow"):
        super().__init__(window)
        self.window = window
        self.original = window.profile
        self.setWindowTitle("Compute limits")
        self.site = QLineEdit(self.original.site)
        self.threads = QSpinBox()
        self.threads.setRange(1, MAX_THREADS)
        self.threads.setValue(self.original.threads)
        self.duty = QSpinBox()
        self.duty.setRange(1, 100)
        self.duty.setSuffix("%")
        self.duty.setValue(self.original.duty)
        self.effective = QLabel(window.effective_limits())
        self.error = QLabel()
        self.error.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout = QFormLayout(self)
        layout.addRow("&Site address", self.site)
        layout.addRow("CPU &threads", self.threads)
        layout.addRow("Compute &duty cycle", self.duty)
        layout.addRow("Effective", self.effective)
        layout.addRow("GPU memory", QLabel("Unavailable for the current CPU worker"))
        layout.addRow("Capacity", QLabel("One active task and one assigned block"))
        if window.thread:
            layout.addRow(QLabel("Pause and start the worker to apply saved changes."))
        layout.addRow(self.error)
        layout.addRow(buttons)

    def save(self):
        values = {"site": self.site.text(), "threads": self.threads.value(), "duty": self.duty.value()}
        try:
            self.window.apply_profile(self.window.store.update({
                name: value for name, value in values.items() if value != getattr(self.original, name)
            }))
        except (OSError, ValueError) as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class NodeWindow(QWidget):
    def __init__(self, launch_in_background: bool, store: ProfileStore | None = None):
        super().__init__()
        self.store = store or ProfileStore()
        profile_error = ""
        try:
            self.profile = self.store.read()
        except (OSError, ValueError) as exc:
            self.profile = ContributorProfile()
            profile_error = f"Could not read compute settings · {exc}"
        self.thread = None
        self.quitting = False
        self.setWindowTitle("NeuroGrid Node")
        self.resize(480, 365)
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Create a node key on the Downloads page")
        self.load_key()
        self.autostart = QCheckBox("Launch and resume when I sign in")
        self.autostart.setChecked(self.profile.autostart)
        self.autostart.toggled.connect(self.toggle_autostart)
        self.info = QLabel(profile_error or "Paused · select Start to share compute")
        self.info.setWordWrap(True)
        self.info.setAccessibleName("Node status")
        self.toggle = QPushButton("Start")
        self.toggle.clicked.connect(self.toggle_compute)
        self.quit = QPushButton("Quit")
        self.quit.clicked.connect(self.request_quit)
        self.saved_limits = QLabel()
        self.saved_limits.setWordWrap(True)
        self.connection = QLabel()
        self.connection.setWordWrap(True)
        settings = QPushButton("Settings")
        settings_menu = QMenu(settings)
        settings_menu.addAction("Compute limits", self.open_limits)
        settings.setMenu(settings_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        title = QLabel("NeuroGrid Node")
        font = title.font()
        font.setPointSize(20)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        layout.addWidget(QLabel("Host 10 layers of Granite 4.2 8B on your CPU."))
        form = QFormLayout()
        form.addRow("Node &key", self.key)
        layout.addLayout(form)
        layout.addWidget(self.saved_limits)
        layout.addWidget(self.connection)
        layout.addWidget(self.autostart)
        layout.addWidget(self.info)
        controls = QHBoxLayout()
        controls.addWidget(self.toggle)
        controls.addWidget(settings)
        controls.addWidget(self.quit)
        layout.addLayout(controls)
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(icon, self)
            self.tray.setToolTip("NeuroGrid Node")
            menu = QMenu(self)
            menu.addAction("Show NeuroGrid", self.show_window)
            menu.addAction("Compute limits", self.open_limits)
            self.tray_toggle = QAction("Start", self)
            self.tray_toggle.triggered.connect(self.toggle_compute)
            menu.addAction(self.tray_toggle)
            menu.addSeparator()
            menu.addAction("Quit", self.request_quit)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self.tray_activated)
            self.tray.show()
        self.refresh_limits()
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh_limits)
        self.status_timer.start(3000)
        if profile_error or not launch_in_background or not self.tray or not self.key.text():
            self.show()
        if (not profile_error and launch_in_background and self.profile.autostart
                and not self.profile.paused and self.key.text()):
            QTimer.singleShot(0, self.toggle_compute)

    def load_key(self):
        self.key.clear()
        try:
            key = (keyring.get_password(SERVICE, self.profile.site)
                   or keyring.get_password(SERVICE, self.profile.site + "/"))
            self.key.setText(key or "")
        except (KeyringError, RuntimeError):
            pass

    def effective_limits(self) -> str:
        if self.thread:
            return describe(self.thread.profile)
        if self.external_worker and self.observed:
            return describe(self.observed)
        return "Starting" if self.external_worker else "Worker stopped"

    def refresh_limits(self):
        try:
            running, self.observed = self.store.worker_status()
        except (OSError, ValueError):
            running, self.observed = False, None
        self.external_worker = running and self.thread is None
        self.saved_limits.setText(
            f"{self.profile.site}\nSaved: {describe(self.profile)}\nEffective: {self.effective_limits()}"
        )
        if not self.observed:
            self.connection.clear()
            return
        details = [f"Local worker: {self.observed.state.replace('_', ' ')}"] if running else []
        if running and self.observed.shard is not None:
            details.append(f"Partition {self.observed.shard + 1}")
        if self.observed.last_heartbeat:
            time = datetime.fromtimestamp(self.observed.last_heartbeat).strftime("%Y-%m-%d %H:%M:%S")
            details.append(f"Last heartbeat {time}")
        self.connection.setText(" · ".join(details))

    def apply_profile(self, profile: ContributorProfile):
        previous_site = self.profile.site
        self.profile = profile
        if profile.site != previous_site:
            self.load_key()
        self.refresh_limits()

    def open_limits(self):
        self.show_window()
        try:
            self.apply_profile(self.store.read())
        except (OSError, ValueError) as exc:
            self.status(f"Could not read compute settings · {exc}")
            return
        dialog = ComputeLimitsDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window()

    def status(self, message: str):
        self.info.setText(message)
        if self.tray:
            self.tray.setToolTip("NeuroGrid Node\n" + message)

    def label_toggle(self, text: str, enabled: bool = True):
        self.toggle.setText(text)
        self.toggle.setEnabled(enabled)
        if self.tray:
            self.tray_toggle.setText(text)
            self.tray_toggle.setEnabled(enabled)

    def toggle_compute(self):
        if self.thread:
            try:
                self.apply_profile(self.store.update({"paused": True}))
            except (OSError, ValueError) as exc:
                self.status(f"Could not save pause · {exc}")
                return
            self.stop_compute()
            return
        try:
            self.apply_profile(self.store.read())
            if self.external_worker:
                self.status("A headless worker is using this profile. Stop it before starting compute here.")
                return
            client = GridClient(self.profile.site, self.key.text())
            if self.profile.paused:
                self.apply_profile(self.store.update({"paused": False}))
        except (OSError, ValueError) as exc:
            self.status(str(exc))
            return
        try:
            keyring.set_password(SERVICE, self.profile.site, self.key.text().strip())
        except (KeyringError, RuntimeError):
            self.status("System keyring unavailable · the key is kept for this session")
        self.thread = ComputeThread(client, self.profile, self.store)
        self.thread.status.connect(self.status)
        self.thread.finished.connect(self.finished)
        self.key.setEnabled(False)
        self.refresh_limits()
        self.label_toggle("Pause")
        self.thread.start()

    def stop_compute(self):
        if self.thread:
            self.status("Pausing · waiting for the current operation")
            self.label_toggle("Pausing…", False)
            self.thread.stop.set()

    def finished(self):
        if self.thread.stop.is_set():
            self.status("Paused · select Start to resume")
        self.thread.deleteLater()
        self.thread = None
        self.key.setEnabled(True)
        self.refresh_limits()
        self.label_toggle("Start")
        if self.quitting:
            QApplication.quit()

    def toggle_autostart(self, enabled: bool):
        try:
            set_autostart(enabled)
            try:
                self.apply_profile(self.store.update({"autostart": enabled}))
            except (OSError, ValueError):
                set_autostart(not enabled)
                raise
        except (OSError, ValueError):
            self.autostart.blockSignals(True)
            self.autostart.setChecked(not enabled)
            self.autostart.blockSignals(False)
            self.status("Could not update automatic startup in this desktop session")

    def request_quit(self):
        self.quitting = True
        if self.thread:
            self.stop_compute()
        else:
            QApplication.quit()

    def closeEvent(self, event: QCloseEvent):
        event.ignore()
        if self.tray:
            self.hide()
        else:
            self.request_quit()


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    smoke = "--smoke-test" in sys.argv
    window = NodeWindow("--autostart" in sys.argv and not smoke)
    if smoke:
        QTimer.singleShot(250, window.request_quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
