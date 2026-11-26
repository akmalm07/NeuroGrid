"""Run the small-transformer prover and its independent verifier on loopback."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock
from zipfile import ZIP_DEFLATED, ZipFile


PROOFS = Path(__file__).resolve().parent
ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/style.css": ("style.css", "text/css"), "/client-verifier.js": ("client-verifier.js", "text/javascript")}
VERIFIER = ("segmented_receipt.py", "granite_layer_proof.py")


def command(script: Path, *args, cwd: Path) -> dict:
    result = subprocess.run([sys.executable, str(script), *map(str, args)], cwd=cwd, check=True,
                            capture_output=True, text=True, encoding="utf-8", timeout=900)
    reports = [line for line in result.stdout.splitlines() if line.startswith("{")]
    return json.loads(reports[-1]) if reports else {}


class Demo:
    def __init__(self, root: Path, scratch: Path, browser_assets: Path | None):
        self.root, self.scratch = root, scratch
        self.browser_assets = browser_assets
        self.public = scratch / "verifier" / "public"
        self.public.mkdir(parents=True)
        for name in VERIFIER:
            shutil.copyfile(PROOFS / name, self.public.parent / name)
        source = root / "public"
        manifest_text = (source / "manifest.json").read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
        if (manifest["schema"] != "neurogrid-segmented-q18-v1" or manifest["vocab"] != 4
                or len(manifest["prompt_edges"]) != 2 or len(manifest["reply_edges"]) != 2):
            raise ValueError("The demo requires the small two-token benchmark")
        (self.public / "manifest.json").write_text(manifest_text, encoding="utf-8")
        shutil.copyfile(source / "params.srs", self.public / "params.srs")
        for op in manifest["circuits"]:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", op):
                raise ValueError("Invalid public verification artifact")
            for extension in ("vk", "json"):
                name = f"{op}.{extension}"
                shutil.copyfile(source / name, self.public / name)
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        self.pin = sha256(canonical).hexdigest()
        self.lock = Lock()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.jobs = {}

    def submit(self, tokens: list[int], nonce: str) -> str | None:
        with self.lock:
            if any(job["state"] in ("proving", "verifying") for job in self.jobs.values()):
                return None
            if len(self.jobs) >= 8:
                oldest = next(iter(self.jobs))
                shutil.rmtree(self.scratch / oldest)
                del self.jobs[oldest]
            job_id = secrets.token_hex(16)
            self.jobs[job_id] = {"state": "proving", "prompt": tokens, "manifest": self.pin}
            self.pool.submit(self.run, job_id, tokens, nonce)
            return job_id

    def run(self, job_id: str, tokens: list[int], nonce: str) -> None:
        folder = self.scratch / job_id
        try:
            folder.mkdir()
            prover = PROOFS / "segmented_benchmark.py"
            request, bundle = (folder / filename for filename in ("request.json", "bundle.json"))
            request.write_text(json.dumps({"nonce": nonce, "prompt": tokens, "manifest": self.pin}), encoding="utf-8")
            command(prover, "prove", self.root, bundle, request, cwd=PROOFS)
            self.update(job_id, state="verifying")
            # The verifier gets no model graph, proving key or witness. The request
            # and verifier artifacts come from the server, and the bundle from the prover.
            checked = command(self.public.parent / VERIFIER[0], self.public, request, bundle, cwd=self.public.parent)
            if checked.get("phase") != "verify":
                raise RuntimeError("Independent verifier returned no result")
            with ZipFile(folder / "verification.zip", "w", compression=ZIP_DEFLATED) as archive:
                for artifact in (*self.public.iterdir(), *(self.public.parent / name for name in VERIFIER)):
                    archive.write(artifact, artifact.relative_to(self.public.parent))
                archive.write(request, "request.json")
                archive.write(bundle, "bundle.json")
            self.update(job_id, state="verified", **checked)
        except Exception as error:
            print(f"Job {job_id} failed: {error}", file=sys.stderr, flush=True)
            self.update(job_id, state="failed")

    def update(self, job_id: str, **changes) -> None:
        with self.lock:
            self.jobs[job_id].update(changes)


class Handler(BaseHTTPRequestHandler):
    def send_data(self, status: int, mime: str, body: bytes, filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, body: dict) -> None:
        self.send_data(status, "application/json", json.dumps(body).encode())

    def valid_origin(self) -> bool:
        host = f"127.0.0.1:{self.server.server_port}"
        origin = self.headers.get("Origin")
        return self.headers.get("Host") == host and origin in (None, f"http://{host}")

    def do_GET(self) -> None:
        if not self.valid_origin():
            self.send_json(403, {"error": "Loopback origin required."})
            return
        if self.path in ASSETS:
            filename, mime = ASSETS[self.path]
            self.send_data(200, mime + "; charset=utf-8", (PROOFS / "web_demo" / filename).read_bytes())
            return
        if self.path == "/favicon.ico":
            self.send_data(204, "image/x-icon", b"")
            return
        if self.path in ("/halo2.js", "/halo2_bg.wasm") and self.demo.browser_assets:
            artifact = self.demo.browser_assets / self.path[1:]
            mime = "application/wasm" if artifact.suffix == ".wasm" else "text/javascript"
            self.send_data(200, mime, artifact.read_bytes())
            return
        parts = self.path.split("/")
        if len(parts) == 4 and parts[1:3] == ["api", "model"]:
            names = {"manifest.json", "params.srs"}
            model = self.demo.public
            for op in json.loads((model / "manifest.json").read_text(encoding="utf-8"))["circuits"]:
                names.update((f"{op}.vk", f"{op}.json"))
            if parts[3] in names:
                artifact = model / parts[3]
                self.send_data(200, "application/octet-stream", artifact.read_bytes())
                return
        if parts[1:3] == ["api", "jobs"] and len(parts) in (4, 5):
            job_id = parts[3]
            bundle = None
            with self.demo.lock:
                job = self.demo.jobs.get(job_id)
                if job and len(parts) == 4:
                    payload = job.copy()
                elif job and parts[4:] == ["bundle"] and job["state"] == "verified":
                    bundle = (self.demo.scratch / job_id / "verification.zip").read_bytes()
                    payload = None
                elif job and parts[4:] == ["receipt"] and job["state"] == "verified":
                    payload = json.loads((self.demo.scratch / job_id / "bundle.json").read_text(encoding="utf-8"))
                else:
                    payload = None
            if bundle is not None:
                self.send_data(200, "application/zip", bundle, "neurogrid-verification.zip")
                return
            if payload:
                self.send_json(200, payload)
                return
        self.send_json(404, {"error": "Unknown resource."})

    def do_POST(self) -> None:
        if not self.valid_origin():
            self.send_json(403, {"error": "Loopback origin required."})
            return
        if self.path != "/api/jobs":
            self.send_json(404, {"error": "Unknown resource."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if self.headers.get_content_type() != "application/json" or not 1 <= size <= 256:
                raise ValueError("Invalid request framing")
            payload = json.loads(self.rfile.read(size))
            tokens, nonce = payload["prompt"], payload["nonce"]
            if set(payload) != {"prompt", "nonce"} or not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{64}", nonce):
                raise ValueError("Invalid browser nonce")
            if not isinstance(tokens, list) or len(tokens) != 2 or any(
                    type(token) is not int or not 0 <= token < 4 for token in tokens):
                raise ValueError("Invalid tokens")
        except (ValueError, KeyError, TypeError, RecursionError):
            self.send_json(400, {"error": "Choose two token IDs from 0 to 3."})
            return
        job_id = self.demo.submit(tokens, nonce)
        if job_id is None:
            self.send_json(409, {"error": "Wait for the active proof to finish."})
        else:
            self.send_json(202, {"job": job_id})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--browser-assets", type=Path, required=True)
    args = parser.parse_args()
    for name in ("halo2.js", "halo2_bg.wasm"):
        if not (args.browser_assets / name).is_file():
            parser.error("Build the browser verifier assets before running the demo")
    with TemporaryDirectory(prefix="neurogrid-demo-") as scratch:
        demo = Demo(args.root.resolve(), Path(scratch), args.browser_assets.resolve())

        class DemoHandler(Handler):
            pass

        DemoHandler.demo = demo
        try:
            with ThreadingHTTPServer(("127.0.0.1", args.port), DemoHandler) as server:
                print(f"Demo listening at http://127.0.0.1:{server.server_port}", flush=True)
                server.serve_forever()
        finally:
            demo.pool.shutdown()


if __name__ == "__main__":
    main()
