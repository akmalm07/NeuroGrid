"use strict";

const form = document.querySelector("#prompt");
const button = form.querySelector("button");
const status = document.querySelector("#status");
const result = document.querySelector("#result");
const download = document.querySelector("#download");
const modelPin = "ae0b6788c8938824f6fee3c773e44f0e92105790cd5d61e07f4bd8632b11ee22";

async function json(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error);
  return data;
}

function checkProof(request, receipt) {
  return new Promise((resolve, reject) => {
    const worker = new Worker("/client-verifier.js", { type: "module" });
    worker.onmessage = ({ data }) => {
      worker.terminate();
      if (data.error) reject(new Error(data.error));
      else resolve(data.verified);
    };
    worker.onerror = () => {
      worker.terminate();
      reject(new Error("The browser verifier could not start."));
    };
    worker.postMessage({ request, receipt });
  });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  result.hidden = true;
  status.textContent = "Starting proof generation.";
  const prompt = ["#first", "#second"].map((id) => Number(form.querySelector(id).value));
  try {
    const manifest = new URLSearchParams(location.search).get("manifest") ?? modelPin;
    if (!/^[a-f0-9]{64}$/.test(manifest)) throw new Error("Invalid trusted model fingerprint.");
    const nonce = Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2, "0")).join("");
    const request = { nonce, prompt, manifest };
    const { job } = await json("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, nonce }),
    });
    const url = `/api/jobs/${job}`;
    for (;;) {
      const progress = await json(url);
      if (progress.state === "verified") {
        status.textContent = "Checking every proof segment in this browser.";
        const proof = await checkProof(request, await json(`${url}/receipt`));
        status.textContent = "This browser independently verified the complete response.";
        document.querySelector("#reply").textContent = proof.reply.join(" · ");
        document.querySelector("#segments").textContent = proof.segments;
        document.querySelector("#seconds").textContent = `${proof.seconds.toFixed(2)} s`;
        document.querySelector("#manifest").textContent = manifest;
        download.href = `${url}/bundle`;
        result.hidden = false;
        break;
      }
      if (progress.state === "failed") throw new Error("The proof job failed. Check the demo server log.");
      status.textContent = progress.state === "verifying"
        ? "The server is preparing the proof bundle."
        : "Generating the response and its proof. This can take a few minutes.";
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  } catch (error) {
    status.textContent = error.message || "Unable to reach the demo server.";
  } finally {
    button.disabled = false;
  }
});
