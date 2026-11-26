import { Field, Poseidon, verify } from 'o1js'
import { z } from 'zod'
import manifest from '../neurogrid_node/granite.json' with { type: 'json' }
import key from './granite-projection-key.json' with { type: 'json' }

export const GRANITE_PROJECTION = 'granite-q-projection-layer0-row0-v1'
export const { revision: GRANITE_REVISION, width: WIDTH } = manifest
export const OFFSET = 32768
const accumulatorBound = WIDTH * OFFSET * OFFSET
const accumulator = z.number().int().min(-accumulatorBound).max(accumulatorBound)
const field = z.string().refine((value) => /^(0|[1-9][0-9]{0,76})$/.test(value) && BigInt(value) < Field.ORDER)
export const projectionBlindSchema = field.refine((value) => value !== '0')
export const projectionAssignmentSchema = z.strictObject({
  operation: z.literal(GRANITE_PROJECTION),
  revision: z.literal(GRANITE_REVISION),
  job: z.uuidv4().refine((value) => value === value.toLowerCase()),
  input: field,
})
const receiptSchema = z.strictObject({
  output: accumulator,
  proof: z.string().min(1).max(64_000).regex(/^[A-Za-z0-9+/]+={0,2}$/),
})

export type ProjectionAssignment = z.infer<typeof projectionAssignmentSchema>
export type ProjectionReceipt = z.infer<typeof receiptSchema>

export function packActivation(lanes: Field[]) {
  const packed: Field[] = []
  // Fifteen 16-bit lanes fit in one field element without reduction.
  for (let start = 0; start < lanes.length; start += 15) {
    packed.push(lanes.slice(start, start + 15)
      .reduceRight((word, lane) => word.mul(65536).add(lane), Field(0)))
  }
  return packed
}

export function projectionInput(job: Field, shifted: Field[], blind: Field) {
  return Poseidon.hash([Field(20), job, blind, ...packActivation(shifted)])
}

export function projectionDigest(job: Field, input: Field, output: Field) {
  return Poseidon.hash([Field(21), Field(BigInt(`0x${GRANITE_REVISION}`)), job, input, output])
}

export function projectionJob(assignment: ProjectionAssignment) {
  return Field(BigInt(`0x${assignment.job.replaceAll('-', '')}`))
}

export async function verifyGraniteProjection(assignmentValue: unknown, receiptValue: unknown) {
  const assignment = projectionAssignmentSchema.safeParse(assignmentValue)
  const receipt = receiptSchema.safeParse(receiptValue)
  if (!assignment.success || !receipt.success) return false
  const digest = projectionDigest(projectionJob(assignment.data), Field(assignment.data.input), Field(receipt.data.output))
  try {
    return await verify({
      proof: receipt.data.proof, publicInput: [digest.toString()], publicOutput: [], maxProofsVerified: 0,
    }, key.data)
  } catch {
    return false
  }
}
