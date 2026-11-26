import { readFile } from 'node:fs/promises'
import { WASI } from 'node:wasi'
import { resolve } from 'node:path'
import { performance } from 'node:perf_hooks'

const [binary, folder, ...operations] = process.argv.slice(2)
if (!binary || !folder || !operations.length || operations.some((op) => !['prove', 'verify'].includes(op))) {
  throw new Error('Usage: node wasi.mjs module.wasm job-dir prove|verify [prove|verify ...]')
}
const start = performance.now()
const module = await WebAssembly.compile(await readFile(binary))
console.log(JSON.stringify({ phase: 'wasm_load', seconds: (performance.now() - start) / 1000 }))
for (const operation of operations) {
  const begin = performance.now()
  const wasi = new WASI({ version: 'preview1', args: ['tensor', operation], env: {},
    preopens: { '/job': resolve(folder) }, returnOnExit: true })
  const instance = await WebAssembly.instantiate(module, wasi.getImportObject())
  const result = wasi.start(instance)
  console.log(JSON.stringify({ phase: 'wasi_wall', operation, seconds: (performance.now() - begin) / 1000,
    peak_resident_bytes: process.resourceUsage().maxRSS * 1024 }))
  if (result !== 0) process.exit(result)
}
