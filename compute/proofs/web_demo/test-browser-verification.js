// Run using playwright-cli run-code --filename after opening the local demo.
async page => {
  const origin = new URL(page.url()).origin;
  let issued;
  let received;
  await page.route(`${origin}/api/jobs`, async route => {
    issued = route.request().postDataJSON();
    await route.continue();
  });
  await page.route("**/api/jobs/*/receipt", async route => {
    const response = await route.fetch();
    received = await response.json();
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "Generate and verify" }).click();
  await page.getByText("This browser independently verified the complete response.").waitFor({ timeout: 300_000 });
  if ((await page.locator("#segments").innerText()) !== "54" || !issued || !received) {
    throw new Error("Browser did not report all 54 proofs");
  }
  const intact = await page.locator("#status").innerText();

  // Replay the same authorized request from a server that reports success but
  // delivers an altered cryptographic proof. The unchanged request bypasses
  // metadata checks and reaches the WASM proof verification.
  await page.evaluate(nonce => {
    const getRandomValues = crypto.getRandomValues.bind(crypto);
    const requestBytes = Uint8Array.from(nonce.match(/../g), v => Number.parseInt(v, 16));
    crypto.getRandomValues = array => {
      if (array instanceof Uint8Array && array.length === requestBytes.length) {
        array.set(requestBytes);
        return array;
      }
      return getRandomValues(array);
    };
  }, issued.nonce);
  const changed = structuredClone(received);
  const proof = changed.segments[0].proof;
  const offset = 48;
  changed.segments[0].proof = proof.slice(0, offset) + (proof[offset] === "A" ? "B" : "A") + proof.slice(offset + 1);
  await page.unroute(`${origin}/api/jobs`);
  await page.unroute("**/api/jobs/*/receipt");
  await page.route(`${origin}/api/jobs`, route => route.fulfill({ json: { job: "adversarial-fixture" }, status: 202 }));
  await page.route("**/api/jobs/adversarial-fixture", route => route.fulfill({
    json: { state: "verified", reply: received.reply, segments: 54 },
  }));
  await page.route("**/api/jobs/adversarial-fixture/receipt", route => route.fulfill({ json: changed }));
  await page.getByRole("button", { name: "Generate and verify" }).click();
  await page.getByText(/Browser rejected proof segment/).waitFor({ timeout: 60_000 });
  if (await page.locator("#result").isVisible()) throw new Error("The site displayed an unverified result");
  const report = { genuine: intact, modified: await page.locator("#status").innerText(), segments: 54 };
  await page.unrouteAll({ behavior: "wait" });
  await page.reload();
  return report;
}
