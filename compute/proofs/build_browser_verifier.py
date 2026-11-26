"""Build the browser verifier from a clean checkout of the pinned EZKL revision."""

import argparse
from pathlib import Path
import subprocess


EZKL_REVISION = "534ff3e6c13d4c2c1aca2358910d62dac466d3cc"
WASM_BINDGEN_VERSION = "wasm-bindgen 0.2.108"
HOOK = '\n#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]\nmod neurogrid_browser_verifier;\n'


def output(*args: str, cwd: Path) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def build(source: Path, target: Path, bindgen: str) -> None:
    if output("git", "rev-parse", "HEAD", cwd=source) != EZKL_REVISION or output(
            "git", "status", "--porcelain", cwd=source):
        raise ValueError("Use a clean EZKL checkout at the pinned 23.0.5 revision")
    if output(bindgen, "--version", cwd=source) != WASM_BINDGEN_VERSION:
        raise ValueError("Use wasm-bindgen-cli 0.2.108 to match the EZKL lockfile")
    target.mkdir(parents=True, exist_ok=True)
    lib = source / "src" / "lib.rs"
    adapter = source / "src" / "neurogrid_browser_verifier.rs"
    original = lib.read_bytes()
    with adapter.open("xb") as module:
        module.write((Path(__file__).with_name("web_demo") / "halo2_browser.rs").read_bytes())
    try:
        lib.write_bytes(original + HOOK.encode())
        subprocess.run(["cargo", "+nightly-2025-12-01", "build", "--locked", "--release", "--lib",
                        "--target", "wasm32-unknown-unknown", "--no-default-features",
                        "--features", "reusable-verifier"], cwd=source, check=True)
        wasm = source / "target" / "wasm32-unknown-unknown" / "release" / "ezkl.wasm"
        subprocess.run([bindgen, "--target", "web", "--out-name", "halo2", "--out-dir", str(target), str(wasm)],
                       cwd=source, check=True)
    finally:
        lib.write_bytes(original)
        adapter.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--bindgen", default="wasm-bindgen")
    args = parser.parse_args()
    build(args.source.resolve(), args.target.resolve(), args.bindgen)


if __name__ == "__main__":
    main()
