import { createHash, randomUUID } from 'node:crypto'
import { Field, Gadgets, Provable, ZkProgram, setNumberOfWorkers } from 'o1js'
import { z } from 'zod'
import {
  GRANITE_PROJECTION, GRANITE_REVISION, OFFSET, WIDTH, projectionAssignmentSchema, projectionBlindSchema,
  projectionDigest, projectionInput, projectionJob,
  type ProjectionAssignment, type ProjectionReceipt,
} from './granite-receipt.ts'
import key from './granite-projection-key.json' with { type: 'json' }

const lanes = z.array(z.number().int().min(-OFFSET).max(OFFSET - 1)).length(WIDTH)
const sampleSchema = z.strictObject({
  revision: z.literal(GRANITE_REVISION),
  coefficientSha256: z.string().regex(/^[a-f0-9]{64}$/),
  coefficients: lanes,
  activation: lanes,
  expected: z.number().int(),
})
const Activation = Provable.Array(Field, WIDTH)

function sample(value: unknown) {
  const artifact = sampleSchema.parse(value)
  const bytes = Buffer.alloc(WIDTH * 2)
  artifact.coefficients.forEach((lane, i) => bytes.writeInt16LE(lane, i * 2))
  if (createHash('sha256').update(bytes).digest('hex') !== artifact.coefficientSha256) {
    throw new Error('Granite projection coefficients do not match their fingerprint')
  }
  return artifact
}

export function graniteCircuit(coefficients: readonly number[]) {
  return ZkProgram({
    name: GRANITE_PROJECTION,
    publicInput: Field,
    methods: {
      project: {
        privateInputs: [Field, Activation, Field, Field],
        async method(digest, job, shifted, blind, output) {
          blind.assertNotEquals(0)
          let weighted = Field(0)
          let correction = 0
          for (const [i, lane] of shifted.entries()) {
            Gadgets.rangeCheck16(lane)
            weighted = weighted.add(lane.mul(coefficients[i]))
            // Materialize partial sums so the compiler never traverses one 4096-deep expression.
            if ((i + 1) % 64 === 0) weighted = weighted.seal()
            correction += OFFSET * coefficients[i]
          }
          weighted.sub(correction).assertEquals(output)
          const input = projectionInput(job, shifted, blind)
          projectionDigest(job, input, output).assertEquals(digest)
        },
      },
    },
  })
}

export async function exportGraniteKey(value: unknown) {
  const { coefficientSha256, coefficients } = sample(value)
  setNumberOfWorkers(2)
  const { verificationKey } = await graniteCircuit(coefficients).compile()
  return { coefficientSha256, data: verificationKey.data }
}

export function issueGraniteProjection(value: unknown) {
  const { activation } = sample(value)
  const assignment: ProjectionAssignment = {
    operation: GRANITE_PROJECTION, revision: GRANITE_REVISION, job: randomUUID(), input: '0',
  }
  let blind = Field.random()
  while (blind.toBigInt() === 0n) blind = Field.random()
  assignment.input = projectionInput(projectionJob(assignment),
    activation.map((lane) => Field(lane + OFFSET)), blind).toString()
  return { assignment, opening: blind.toString() }
}

export async function proveGraniteProjection(value: unknown, assignmentValue: unknown, openingValue: unknown) {
  const { coefficientSha256, coefficients, activation, expected } = sample(value)
  if (coefficientSha256 !== key.coefficientSha256) throw new Error('Unrecognized Granite projection coefficients')
  const assignment = projectionAssignmentSchema.parse(assignmentValue)
  const job = projectionJob(assignment)
  const shifted = activation.map((lane) => Field(lane + OFFSET))
  const blind = Field(projectionBlindSchema.parse(openingValue))
  const input = projectionInput(job, shifted, blind)
  if (input.toString() !== assignment.input) throw new Error('Projection input does not match the assignment')

  // All 4096 int16 products and their sum fit exactly in JavaScript's safe integer range.
  const output = activation.reduce((sum, lane, i) => sum + lane * coefficients[i], 0)
  if (output !== expected) throw new Error('Projection does not match the checkpoint benchmark')
  const digest = projectionDigest(job, input, Field(output))

  setNumberOfWorkers(2)
  const circuit = graniteCircuit(coefficients)
  const compileStart = performance.now()
  const { verificationKey } = await circuit.compile()
  if (verificationKey.data !== key.data) throw new Error('Projection circuit does not match the pinned key')
  const compileMs = performance.now() - compileStart
  const start = performance.now()
  const { proof } = await circuit.project(digest, job, shifted, blind, Field(output))
  const receipt: ProjectionReceipt = { output, proof: proof.toJSON().proof }
  return { receipt, compileMs, proveMs: performance.now() - start }
}
