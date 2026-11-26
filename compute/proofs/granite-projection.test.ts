import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { copyFile, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { before, describe, it } from 'node:test'
import { promisify } from 'node:util'
import { issueGraniteProjection, proveGraniteProjection } from './granite-prover.ts'
import { verifyGraniteProjection } from './granite-receipt.ts'

const samplePath = process.env.NEUROGRID_PROJECTION_SAMPLE
describe('Checkpoint-backed Granite projection proof', {
  skip: !samplePath && 'Set NEUROGRID_PROJECTION_SAMPLE to the artifact exported from the pinned checkpoint',
  timeout: 600_000,
}, () => {
  let sample: { expected: number; coefficientSha256: string; coefficients: number[]; activation: number[] }
  let issued: ReturnType<typeof issueGraniteProjection>
  let result: Awaited<ReturnType<typeof proveGraniteProjection>>

  before(async () => {
    sample = JSON.parse(await readFile(samplePath!, 'utf8'))
    issued = issueGraniteProjection(sample)
    result = await proveGraniteProjection(sample, issued.assignment, issued.opening)
  })

  it('verifies the exact checkpoint-derived integer result in a fresh process without activation or weights', async (t) => {
    assert.equal(result.receipt.output, sample.expected)
    const dir = await mkdtemp(join(tmpdir(), 'neurogrid-granite-verifier-'))
    try {
      await mkdir(join(dir, 'proofs'))
      await mkdir(join(dir, 'neurogrid_node'))
      for (const file of ['cli.ts', 'granite-receipt.ts', 'granite-projection-key.json']) {
        await copyFile(new URL(file, import.meta.url), join(dir, 'proofs', file))
      }
      await copyFile(new URL('../neurogrid_node/granite.json', import.meta.url), join(dir, 'neurogrid_node', 'granite.json'))
      await writeFile(join(dir, 'package.json'), '{"type":"module"}\n')
      await symlink(resolve('node_modules'), join(dir, 'node_modules'), 'junction')
      await writeFile(join(dir, 'assignment.json'), JSON.stringify(issued.assignment))
      await writeFile(join(dir, 'receipt.json'), JSON.stringify(result.receipt))
      const { stdout } = await promisify(execFile)(process.execPath, [
        '--experimental-strip-types', 'proofs/cli.ts', 'granite-verify', 'assignment.json', 'receipt.json',
      ], { cwd: dir, timeout: 60_000 })
      assert.match(stdout, /Verified one Granite projection row/)
      t.diagnostic(stdout.trim())
      t.diagnostic(`Proving ${Math.round(result.proveMs)} ms, key setup ${Math.round(result.compileMs)} ms, receipt ${Buffer.byteLength(JSON.stringify(result.receipt))} bytes`)
    } finally {
      await rm(dir, { recursive: true, force: true })
    }
  })

  it('rejects an altered result and a receipt replayed against a fresh assignment', async () => {
    assert.equal(await verifyGraniteProjection(issued.assignment,
      { ...result.receipt, output: result.receipt.output + 1 }), false)
    assert.equal(await verifyGraniteProjection(issueGraniteProjection(sample).assignment, result.receipt), false)
  })

  it('rejects changes to the pinned weight row and the committed activation before proving', async () => {
    const badWeights = structuredClone(sample)
    badWeights.coefficients[0] ^= 1
    const bytes = Buffer.alloc(badWeights.coefficients.length * 2)
    badWeights.coefficients.forEach((lane, index) => bytes.writeInt16LE(lane, index * 2))
    badWeights.coefficientSha256 = createHash('sha256').update(bytes).digest('hex')
    await assert.rejects(proveGraniteProjection(badWeights, issued.assignment, issued.opening), /Unrecognized/)
    const badActivation = structuredClone(sample)
    badActivation.activation[0] ^= 1
    await assert.rejects(proveGraniteProjection(badActivation, issued.assignment, issued.opening), /assignment/)
  })
})
