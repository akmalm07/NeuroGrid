"""Build source installers with platform-appropriate launchers."""

import hashlib
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def make_release(platform: str, output: Path):
    if platform not in {"windows", "linux"}:
        raise ValueError("Expected windows or linux")
    files = {
        "INSTALL.md": ROOT / "packaging" / "INSTALL.md",
        "pyproject.toml": ROOT / "compute" / "pyproject.toml",
    }
    node = ROOT / "compute" / "neurogrid_node"
    files.update({f"neurogrid_node/{path.name}": path for path in node.glob("*.py")})
    files["neurogrid_node/granite.json"] = node / "granite.json"
    files.update({path.name: path for path in (ROOT / "packaging" / platform).iterdir()})
    output.mkdir(parents=True, exist_ok=True)
    if platform == "windows":
        archive = output / "NeuroGrid-Node-Windows-x64.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            for name, source in files.items():
                handle.writestr(name, source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    else:
        archive = output / "NeuroGrid-Node-Linux-x64.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            for name, source in files.items():
                info = handle.gettarinfo(source, arcname=name)
                info.mode = 0o755 if name.endswith(".sh") or name in {"start-neurogrid", "neurogrid-agent"} else 0o644
                data = source.read_bytes().replace(b"\r\n", b"\n")
                info.size = len(data)
                from io import BytesIO
                handle.addfile(info, BytesIO(data))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    print(archive.name)


if __name__ == "__main__":
    make_release(sys.argv[1], Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "release-dist")
