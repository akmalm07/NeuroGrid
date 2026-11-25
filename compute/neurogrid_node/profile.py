"""Durable contributor settings shared by the desktop and CLI."""

import configparser
import errno
import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urlsplit

from . import SHARD_COUNT

DEFAULT_SITE = "https://neurogrid.app.space"
MAX_THREADS = 8
MAX_PROFILE_BYTES = 4096
WorkerState = Literal["connecting", "loading", "online", "computing", "disconnected",
                      "reenrollment_required", "failed", "stopped"]


def site_origin(value: str) -> str:
    message = "Enter the HTTPS site address with no path"
    try:
        value = value.strip()
        if len(value) > 256 or any(char.isspace() or ord(char) < 32 or char in '\\<>"%' for char in value):
            raise ValueError
        parsed = urlsplit(value)
        port = parsed.port
        localhost = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not (localhost and parsed.scheme == "http")
                or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or "?" in value or "#" in value or port == 0):
            raise ValueError
    except ValueError:
        raise ValueError(message) from None
    return f"{parsed.scheme}://{parsed.netloc}"


@dataclass(frozen=True)
class ContributorProfile:
    version: int = 1
    site: str = DEFAULT_SITE
    threads: int = min(2, os.cpu_count() or 1)
    duty: int = 75
    autostart: bool = False
    paused: bool = False

    @classmethod
    def parse(cls, document: object) -> "ContributorProfile":
        if not isinstance(document, dict) or document.keys() != asdict(cls()).keys():
            raise ValueError("Invalid contributor profile fields")
        if type(document["version"]) is not int or document["version"] != 1:
            raise ValueError("Unsupported contributor profile version")
        if not isinstance(document["site"], str):
            raise ValueError("Invalid site address")
        if type(document["threads"]) is not int or not 1 <= document["threads"] <= MAX_THREADS:
            raise ValueError(f"CPU threads must be between 1 and {MAX_THREADS}")
        if type(document["duty"]) is not int or not 1 <= document["duty"] <= 100:
            raise ValueError("Compute duty cycle must be between 1 and 100")
        if type(document["autostart"]) is not bool or type(document["paused"]) is not bool:
            raise ValueError("Invalid contributor startup settings")
        return cls(**{**document, "site": site_origin(document["site"])})


@dataclass(frozen=True)
class WorkerStatus:
    state: WorkerState = "connecting"
    threads: int = 2
    duty: int = 75
    shard: int | None = None
    last_heartbeat: int | None = None


def user_config_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def default_profile_path() -> Path:
    return user_config_dir() / ("NeuroGrid" if os.name == "nt" else "neurogrid") / "profile.json"


def legacy_settings() -> dict[str, object]:
    keys = ("site", "threads", "duty", "autostart")
    if os.name == "nt":
        import winreg

        values = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\NeuroGrid\Node") as key:
                for name in keys:
                    try:
                        values[name] = winreg.QueryValueEx(key, name)[0]
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            pass
        return values
    legacy = configparser.ConfigParser(interpolation=None)
    try:
        legacy.read(user_config_dir() / "NeuroGrid" / "Node.conf", encoding="utf-8")
    except (configparser.Error, UnicodeError):
        raise ValueError("Could not read legacy contributor settings") from None
    return {key: legacy["General"][key] for key in keys if legacy.has_option("General", key)}


def migrated_profile(values: Mapping[str, object]) -> ContributorProfile:
    defaults = ContributorProfile()
    document = asdict(defaults)
    for key in ("site", "threads", "duty", "autostart"):
        value = values.get(key)
        if value is None:
            continue
        try:
            if key in {"threads", "duty"}:
                value = int(str(value))
            if key == "autostart":
                value = str(value).lower() in {"true", "1"}
            document[key] = value
            ContributorProfile.parse(document)
        except ValueError:
            document[key] = getattr(defaults, key)
    return ContributorProfile.parse(document)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Use a process-owned lock so crashes do not leave stale ownership."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "r+b") as handle:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def read_json(path: Path) -> object:
    with path.open("rb") as handle:
        data = handle.read(MAX_PROFILE_BYTES + 1)
    if len(data) > MAX_PROFILE_BYTES:
        raise ValueError("Contributor profile data is too large")
    return json.loads(data)


def write_json(path: Path, document: object) -> None:
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".profile-", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = (path or default_profile_path()).expanduser().resolve()
        self.import_legacy = path is None or self.path == default_profile_path().resolve()

    def read(self) -> ContributorProfile:
        try:
            return self._read()
        except FileNotFoundError:
            return self.update({})

    def _read(self) -> ContributorProfile:
        return ContributorProfile.parse(read_json(self.path))

    def update(self, changes: Mapping[str, object]) -> ContributorProfile:
        with file_lock(self.path.with_suffix(self.path.suffix + ".write.lock")):
            try:
                current = self._read()
                if not changes:
                    return current
            except FileNotFoundError:
                current = migrated_profile(legacy_settings()) if self.import_legacy else ContributorProfile()
            updated = ContributorProfile.parse({**asdict(current), **changes})
            write_json(self.path, asdict(updated))
            return updated

    def worker_status(self) -> tuple[bool, WorkerStatus | None]:
        try:
            with self.worker():
                running = False
        except WorkerBusy:
            running = True
        try:
            document = read_json(self.path.with_name(self.path.name + ".status.json"))
        except FileNotFoundError:
            return running, None
        if not isinstance(document, dict) or document.keys() != asdict(WorkerStatus()).keys():
            raise ValueError("Invalid worker status")
        status = WorkerStatus(**document)
        if (status.state not in get_args(WorkerState)
                or type(status.threads) is not int or not 1 <= status.threads <= MAX_THREADS
                or type(status.duty) is not int or not 1 <= status.duty <= 100
                or status.shard is not None and (type(status.shard) is not int or not 0 <= status.shard < SHARD_COUNT)
                or status.last_heartbeat is not None
                and (type(status.last_heartbeat) is not int or not 0 < status.last_heartbeat < 2**32)):
            raise ValueError("Invalid worker status")
        return running, status

    def report_worker(self, status: WorkerStatus) -> None:
        write_json(self.path.with_name(self.path.name + ".status.json"), asdict(status))

    @contextmanager
    def worker(self) -> Iterator[None]:
        with ExitStack() as stack:
            try:
                stack.enter_context(file_lock(self.path.with_suffix(self.path.suffix + ".worker.lock")))
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                raise WorkerBusy("Could not acquire worker ownership for this profile") from exc
            yield


class WorkerBusy(ValueError):
    pass
