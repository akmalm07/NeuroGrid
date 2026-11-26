import init, { BrowserVerifier, tensor_commitment } from "./halo2.js";

const SCHEMA = "neurogrid-segmented-q18-v1";
const HEX = /^[a-f0-9]{64}$/;
const encoder = new TextEncoder();

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map(key => `${canonical(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, c => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);
}

function toHex(bytes) {
  return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
}

function hexBytes(input) {
  if (typeof input !== "string" || !HEX.test(input)) throw new Error("Invalid tensor commitment");
  return Uint8Array.from(input.match(/../g), part => Number.parseInt(part, 16));
}

async function digest(input) {
  return toHex(new Uint8Array(await crypto.subtle.digest("SHA-256", input)));
}

async function fetchBytes(path, maximum) {
  const response = await fetch(path);
  if (!response.ok) throw new Error("A verification artifact could not be downloaded");
  const length = Number(response.headers.get("Content-Length"));
  if (!Number.isSafeInteger(length) || length <= 0 || length > maximum) throw new Error("Invalid artifact size");
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.length !== length) throw new Error("Incomplete verification artifact");
  return bytes;
}

async function verifiedArtifact(name, pin) {
  const bytes = await fetchBytes(`/api/model/${name}`, 4 * 1024 * 1024);
  if (await digest(bytes) !== pin) throw new Error(`Verification artifact differs from the pinned model: ${name}`);
  return bytes;
}

function openToken(token) {
  const lanes = new Uint8Array(32);
  lanes[0] = token;
  return tensor_commitment(lanes);
}

function tokens(value, count, vocab) {
  if (!Array.isArray(value) || value.length !== count ||
      value.some(token => !Number.isInteger(token) || token < 0 || token >= vocab)) {
    throw new Error("Unexpected token count or token ID");
  }
}

async function contextCommitment(request, segment) {
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(canonical([SCHEMA, request, segment]))));
  const lanes = new Uint8Array(16 * 32);
  for (let i = 0; i < 16; i++) {
    lanes.set(hash.subarray(i * 2, i * 2 + 2), i * 32);
  }
  return tensor_commitment(lanes);
}

async function verify(request, receipt) {
  const start = performance.now();
  const manifest = JSON.parse(new TextDecoder().decode(await fetchBytes("/api/model/manifest.json", 64 * 1024)));
  if (manifest.schema !== SCHEMA || await digest(encoder.encode(canonical(manifest))) !== request.manifest) {
    throw new Error("The model manifest differs from the browser's trusted fingerprint");
  }
  tokens(request.prompt, manifest.prompt_edges.length, manifest.vocab);
  if (receipt.request !== request.nonce || !Array.isArray(receipt.segments) ||
      receipt.segments.length !== manifest.segments.length) {
    throw new Error("The proof does not cover the complete browser request");
  }
  tokens(receipt.reply, manifest.reply_edges.length, manifest.vocab);
  await init();
  const verifier = new BrowserVerifier(await verifiedArtifact("params.srs", manifest.srs_sha256));
  try {
    for (const [operation, pins] of Object.entries(manifest.circuits)) {
      if (!/^[a-z][a-z0-9_-]{0,31}$/.test(operation)) throw new Error("Invalid circuit name");
      const [settings, key] = await Promise.all([
        verifiedArtifact(`${operation}.json`, pins.settings_sha256),
        verifiedArtifact(`${operation}.vk`, pins.vk_sha256),
      ]);
      verifier.add_key(operation, settings, key);
    }
    const known = new Map(Object.entries(manifest.constants));
    manifest.prompt_edges.forEach((name, i) => known.set(name, openToken(request.prompt[i])));
    for (const [i, segment] of manifest.segments.entries()) {
      const item = receipt.segments[i];
      if (!item || item.name !== segment.name || !Array.isArray(item.outputs) ||
          item.outputs.length !== segment.outputs.length || typeof item.proof !== "string" ||
          item.proof.length > 512_000 || segment.outputs.some(name => known.has(name))) {
        throw new Error("Missing or reordered proof segment");
      }
      const context = await contextCommitment(request, segment);
      const fields = [...segment.inputs.map(name => known.get(name)), context, ...item.outputs, context];
      const instances = new Uint8Array(fields.length * 32);
      fields.forEach((field, j) => instances.set(hexBytes(field), j * 32));
      const proof = Uint8Array.from(atob(item.proof), c => c.charCodeAt(0));
      if (!verifier.verify_segment(segment.operation, instances, proof)) {
        throw new Error(`Browser rejected proof segment ${segment.name}`);
      }
      segment.outputs.forEach((name, j) => known.set(name, item.outputs[j]));
    }
    if (manifest.reply_edges.some((name, i) => known.get(name) !== openToken(receipt.reply[i]))) {
      throw new Error("The reply does not open the proved token commitments");
    }
    return { reply: receipt.reply, segments: receipt.segments.length, seconds: (performance.now() - start) / 1000 };
  } finally {
    verifier.free();
  }
}

self.onmessage = async ({ data }) => {
  try {
    self.postMessage({ verified: await verify(data.request, data.receipt) });
  } catch (error) {
    self.postMessage({ error: String(error?.message ?? error) });
  }
};
