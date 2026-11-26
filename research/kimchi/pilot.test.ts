import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { copyFile, mkdtemp, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { before, describe, it } from 'node:test'
import { promisify } from 'node:util'
import { Field } from 'o1js'
import { pilot, runStage, StageClaims, StageWitness } from './circuit.ts'
import { blind, issue, proveReceipt } from './prover.ts'
import { commit, contextFor, domain, stageLease, statementDigest, verifyReceipt } from './receipt.ts'

describe('CPU proof of tiny attention inference', { timeout: 180_000 }, () => {
  const { assignment, opening } = issue(2)
  let result: Awaited<ReturnType<typeof proveReceipt>>

  before(async () => {
    result = await proveReceipt(assignment, opening)
  })

  it('proves the expected autoregressive reply and validates private openings', async () => {
    assert.deepEqual(result.reply.map(({ token }) => token), [3, 3])
    assert.equal(await verifyReceipt(assignment, result.receipt, result.reply), true)
    const changedReply = structuredClone(result.reply)
    changedReply[1].token = 0
    assert.equal(await verifyReceipt(assignment, result.receipt, changedReply), false)
  })

  it('verifies in a new process equipped only with the CLI verifier, public assignment and receipt', async (t) => {
    const dir = await mkdtemp(join(tmpdir(), 'neurogrid-proof-verifier-'))
    try {
      for (const name of ['cli.ts', 'receipt.ts', 'verification-key.json']) {
        await copyFile(new URL(name, import.meta.url), join(dir, name))
      }
      await writeFile(join(dir, 'package.json'), '{"type":"module"}\n')
      await symlink(resolve('node_modules'), join(dir, 'node_modules'), 'junction')
      await writeFile(join(dir, 'assignment.json'), JSON.stringify(assignment))
      await writeFile(join(dir, 'receipt.json'), JSON.stringify(result.receipt))
      const { stdout } = await promisify(execFile)(process.execPath, [
        '--experimental-strip-types', 'cli.ts', 'verify', 'assignment.json', 'receipt.json',
      ], { cwd: dir, timeout: 45_000 })
      assert.match(stdout, /Verified four linked proofs/)
      t.diagnostic(stdout.trim())
      t.diagnostic(`Key setup ${Math.round(result.compileMs)} ms, proving ${Math.round(result.proveMs)} ms, receipt ${Buffer.byteLength(JSON.stringify(result.receipt))} bytes`)
    } finally {
      await rm(dir, { recursive: true, force: true })
    }
  })

  it('rejects replay under another job, lease or epoch', async () => {
    const reissued = issue(2).assignment
    const changedLease = structuredClone(assignment)
    changedLease.leases[0][0] = reissued.leases[0][0]
    for (const changed of [changedLease, { ...assignment, epoch: 1 }, { ...assignment, job: reissued.job }]) {
      assert.equal(await verifyReceipt(changed, result.receipt), false)
    }
  })

  it('rejects wrong model labels, malformed fields and incomplete receipts', async () => {
    assert.equal(await verifyReceipt({ ...assignment, model: 'wrong-model' }, result.receipt), false)
    assert.equal(await verifyReceipt(assignment, { ...result.receipt, steps: result.receipt.steps.slice(0, 1) }), false)
    assert.equal(await verifyReceipt({ ...assignment, input: 'bad-field' }, result.receipt), false)
    assert.equal(await verifyReceipt({ ...assignment, input: Field.ORDER.toString() }, result.receipt), false)
  })

  it('rejects changed or spliced proofs even when the submitted links still agree', async () => {
    const spliced = structuredClone(result.receipt)
    spliced.steps[1][1].proof = result.receipt.steps[0][1].proof
    assert.equal(await verifyReceipt(assignment, spliced), false)
    const wrongActivation = structuredClone(result.receipt)
    wrongActivation.steps[0][0].output = wrongActivation.steps[0][1].input = '1'
    assert.equal(await verifyReceipt(assignment, wrongActivation), false)
    const wrongCache = structuredClone(result.receipt)
    wrongCache.steps[0][0].cacheAfter = wrongCache.steps[1][0].cacheBefore = '1'
    assert.equal(await verifyReceipt(assignment, wrongCache), false)
    const wrongToken = structuredClone(result.receipt)
    wrongToken.steps[1][1].output = '1'
    assert.equal(await verifyReceipt(assignment, wrongToken), false)
  })

  it('rejects a forged activation even with a consistent digest and private opening', async () => {
    const witness = new StageWitness({
      context: contextFor(assignment), position: Field(0), lease: stageLease(assignment, 0, 0),
      input: [Field(2), Field(0)], cache: Array.from({ length: 4 }, () => Field(0)),
      inputBlind: Field(opening.blind), outputBlind: blind(), cacheBeforeBlind: blind(), cacheAfterBlind: blind(),
    })
    const { claims } = runStage(0, witness)
    const wrongOutput = commit(domain.activation, witness.context, Field(0), [Field(6), Field(2)], witness.outputBlind)
    const forged = new StageClaims({ ...claims, output: wrongOutput })
    const digest = statementDigest(witness.context, witness.position, Field(0), witness.lease,
      forged.input, forged.output, forged.cacheBefore, forged.cacheAfter)
    await assert.rejects(pilot.first(digest, witness, forged), /Constraint unsatisfied/)
  })

  it('uses the committed cached value when an earlier key wins attention', () => {
    const witness = new StageWitness({
      context: contextFor(assignment), position: Field(1), lease: stageLease(assignment, 1, 0),
      input: [Field(3), Field(0)], cache: [Field(2), Field(3), Field(3), Field(1)],
      inputBlind: blind(), outputBlind: blind(), cacheBeforeBlind: blind(), cacheAfterBlind: blind(),
    })
    const { output } = runStage(0, witness)
    assert.deepEqual(output.map((lane) => Number(lane.toBigInt())), [4, 3])
  })
})
