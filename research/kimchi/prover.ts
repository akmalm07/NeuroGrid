import { randomUUID } from 'node:crypto'
import { Field, setNumberOfWorkers } from 'o1js'
import { pilot, runStage, StageWitness, type Cache } from './circuit.ts'
import {
  MODEL, SCHEME, INDICES, assignmentSchema, contextFor, openingSchema, receiptSchema, replySchema,
  stageDigest, stageLease, tokenCommitment,
  type Assignment, type Opening, type Receipt,
} from './receipt.ts'
import verificationKey from './verification-key.json' with { type: 'json' }

export function blind(): Field {
  let value = Field.random()
  while (value.toBigInt() === 0n) value = Field.random()
  return value
}

export function issue(inputToken: number): { assignment: Assignment; opening: Opening } {
  const opening = openingSchema.parse({ token: inputToken, blind: blind().toString() })
  const assignment: Assignment = {
    scheme: SCHEME, model: MODEL, job: randomUUID(), epoch: 0,
    leases: [[randomUUID(), randomUUID()], [randomUUID(), randomUUID()]], input: '0',
  }
  assignment.input = tokenCommitment(contextFor(assignment), 0, opening)
  return { assignment, opening }
}

export async function compilePilot() {
  setNumberOfWorkers(2)
  return pilot.compile()
}

export async function proveReceipt(assignmentValue: unknown, openingValue: unknown) {
  const assignment = assignmentSchema.parse(assignmentValue)
  const opening = openingSchema.parse(openingValue)
  const context = contextFor(assignment)
  if (tokenCommitment(context, 0, opening) !== assignment.input) throw new Error('Input opening does not match the assignment')

  const compileStart = performance.now()
  const { verificationKey: compiled } = await compilePilot()
  if (compiled.data !== verificationKey.data) throw new Error('Proof circuit does not match the pinned verification key')
  const compileMs = performance.now() - compileStart
  const proveStart = performance.now()
  const cache: [Cache, Cache] = [[Field(0), Field(0), Field(0), Field(0)], [Field(0), Field(0), Field(0), Field(0)]]
  const cacheBlind = [blind(), blind()]
  let input = [Field(opening.token), Field(0)]
  let inputBlind = Field(opening.blind)
  const steps: [Receipt['steps'][number][number][], Receipt['steps'][number][number][]] = [[], []]
  const reply: Opening[] = []

  for (const position of INDICES) {
    for (const stage of INDICES) {
      const outputBlind = blind()
      const cacheAfterBlind = blind()
      const witness = new StageWitness({
        context, position: Field(position), lease: stageLease(assignment, position, stage),
        input, cache: cache[stage], inputBlind, outputBlind, cacheBeforeBlind: cacheBlind[stage], cacheAfterBlind,
      })
      const { output, claims, nextCache } = runStage(stage, witness)
      const commitments = {
        input: claims.input.toString(), output: claims.output.toString(),
        cacheBefore: claims.cacheBefore.toString(), cacheAfter: claims.cacheAfter.toString(),
      }
      const digest = stageDigest(assignment, position, stage, commitments)
      const method = stage === 0 ? pilot.first : pilot.last
      const { proof } = await method(digest, witness, claims)
      steps[position].push({ ...commitments, proof: proof.toJSON().proof })
      cache[stage] = nextCache
      cacheBlind[stage] = cacheAfterBlind
      input = [output[0], output[1] ?? Field(0)]
      inputBlind = outputBlind
      if (stage === 1) reply.push({ token: Number(output[0].toBigInt()), blind: outputBlind.toString() })
    }
  }

  const receipt = receiptSchema.parse({ scheme: SCHEME, model: MODEL, steps })
  return { receipt, reply: replySchema.parse(reply), compileMs, proveMs: performance.now() - proveStart }
}
