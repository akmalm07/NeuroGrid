"""PySide6 controller for a single volunteer partition."""

import os
import subprocess
import sys
from pathlib import Path
from threading import Event

import keyring
from keyring.errors import KeyringError
from PySide6.QtCore import QSettings, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QSpinBox, QStyle, QSystemTrayIcon, QVBoxLayout, QWidget, QPushButton,
)

from .client import GridClient, GridError, run_node

SERVICE = "NeuroGrid Node"
MODULE = "neurogrid_node.desktop"
DEFAULT_SITE = "https://neurogrid.app.space"


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

    def __init__(self, client, threads, duty):
        super().__init__()
        self.client = client
        self.threads = threads
        self.duty = duty
        self.stop = Event()

    def run(self):
        try:
            run_node(self.client, self.stop, self.threads, self.duty, report=self.status.emit)
        except GridError as exc:
            self.status.emit(
                "Key rejected · create a new key in Downloads" if exc.status == 401
                else f"Coordinator rejected the connection · {exc}",
            )
        except (OSError, RuntimeError, ValueError):
            self.status.emit("Could not load or connect. Check the site, network and model cache, then retry.")


class NodeWindow(QWidget):
    def __init__(self, launch_in_background: bool):
        super().__init__()
        self.settings = QSettings("NeuroGrid", "Node")
        self.thread = None
        self.quitting = False
        self.setWindowTitle("NeuroGrid Node")
        self.resize(480, 365)
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self.site = QLineEdit(self.settings.value("site", DEFAULT_SITE, str))
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Create a node key on the Downloads page")
        try:
            self.key.setText(keyring.get_password(SERVICE, self.site.text()) or "")
        except (KeyringError, RuntimeError):
            pass
        self.threads = QSpinBox()
        self.threads.setRange(1, min(os.cpu_count() or 1, 8))
        self.threads.setValue(self.settings.value("threads", min(2, self.threads.maximum()), int))
        self.duty = QSpinBox()
        self.duty.setRange(10, 100)
        self.duty.setSingleStep(5)
        self.duty.setSuffix("%")
        self.duty.setValue(self.settings.value("duty", 75, int))
        self.autostart = QCheckBox("Launch and resume when I sign in")
        self.autostart.setChecked(self.settings.value("autostart", False, bool))
        self.autostart.toggled.connect(self.toggle_autostart)
        self.info = QLabel("Paused · select Start to share compute")
        self.info.setWordWrap(True)
        self.info.setAccessibleName("Node status")
        self.toggle = QPushButton("Start")
        self.toggle.clicked.connect(self.toggle_compute)
        self.quit = QPushButton("Quit")
        self.quit.clicked.connect(self.request_quit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        title = QLabel("NeuroGrid Node")
        font = title.font()
        font.setPointSize(20)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        layout.addWidget(QLabel("Host 10 layers of SmolLM2-135M-Instruct on your CPU."))
        form = QFormLayout()
        form.addRow("&Site address", self.site)
        form.addRow("Node &key", self.key)
        form.addRow("CPU &threads", self.threads)
        form.addRow("Compute &duty cycle", self.duty)
        layout.addLayout(form)
        layout.addWidget(self.autostart)
        layout.addWidget(self.info)
        controls = QHBoxLayout()
        controls.addWidget(self.toggle)
        controls.addWidget(self.quit)
        layout.addLayout(controls)
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(icon, self)
            self.tray.setToolTip("NeuroGrid Node")
            menu = QMenu(self)
            menu.addAction("Show NeuroGrid", self.show_window)
            self.tray_toggle = QAction("Start", self)
            self.tray_toggle.triggered.connect(self.toggle_compute)
            menu.addAction(self.tray_toggle)
            menu.addSeparator()
            menu.addAction("Quit", self.request_quit)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self.tray_activated)
            self.tray.show()
        if not launch_in_background or not self.tray or not self.key.text():
            self.show()
        if launch_in_background and self.autostart.isChecked() and self.key.text():
            QTimer.singleShot(0, self.toggle_compute)

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

    def controls_enabled(self, enabled: bool):
        for field in (self.site, self.key, self.threads, self.duty):
            field.setEnabled(enabled)

    def label_toggle(self, text: str, enabled: bool = True):
        self.toggle.setText(text)
        self.toggle.setEnabled(enabled)
        if self.tray:
            self.tray_toggle.setText(text)
            self.tray_toggle.setEnabled(enabled)

    def toggle_compute(self):
        if self.thread:
            self.stop_compute()
            return
        try:
            client = GridClient(self.site.text(), self.key.text())
        except ValueError as exc:
            self.status(str(exc))
            return
        self.settings.setValue("site", self.site.text().strip())
        self.settings.setValue("threads", self.threads.value())
        self.settings.setValue("duty", self.duty.value())
        try:
            keyring.set_password(SERVICE, self.site.text().strip(), self.key.text().strip())
        except (KeyringError, RuntimeError):
            self.status("System keyring unavailable · the key is kept for this session")
        self.thread = ComputeThread(client, self.threads.value(), self.duty.value())
        self.thread.status.connect(self.status)
        self.thread.finished.connect(self.finished)
        self.controls_enabled(False)
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
        self.controls_enabled(True)
        self.label_toggle("Start")
        if self.quitting:
            QApplication.quit()

    def toggle_autostart(self, enabled: bool):
        try:
            set_autostart(enabled)
            self.settings.setValue("autostart", enabled)
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
