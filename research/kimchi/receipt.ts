import { Field, Poseidon, verify } from 'o1js'
import { z } from 'zod'
import verificationKey from './verification-key.json' with { type: 'json' }

export const SCHEME = 'kimchi-pickles-v1'
export const MODEL = 'neurogrid-tiny-attention-int-v1'
const field = z.string().refine((value) => /^(0|[1-9][0-9]{0,76})$/.test(value) && BigInt(value) < Field.ORDER)
const uuid = z.uuidv4().refine((value) => value === value.toLowerCase())
const leaseRow = z.tuple([uuid, uuid])
export const assignmentSchema = z.strictObject({
  scheme: z.literal(SCHEME),
  model: z.literal(MODEL),
  job: uuid,
  epoch: z.number().int().min(0).max(0xffffffff),
  leases: z.tuple([leaseRow, leaseRow]),
  input: field,
}).refine(({ leases }) => new Set(leases.flat()).size === 4)

export const openingSchema = z.strictObject({
  token: z.number().int().min(0).max(3),
  blind: field.refine((value) => value !== '0'),
})
export const replySchema = z.tuple([openingSchema, openingSchema])

const commitments = z.strictObject({
  input: field,
  output: field,
  cacheBefore: field,
  cacheAfter: field,
})
const stageReceipt = commitments.extend({
  proof: z.string().min(1).max(64_000).regex(/^[A-Za-z0-9+/]+={0,2}$/),
})
const receiptRow = z.tuple([stageReceipt, stageReceipt])
export const receiptSchema = z.strictObject({
  scheme: z.literal(SCHEME),
  model: z.literal(MODEL),
  steps: z.tuple([receiptRow, receiptRow]),
})

export type Assignment = z.infer<typeof assignmentSchema>
export type Receipt = z.infer<typeof receiptSchema>
export type Opening = z.infer<typeof openingSchema>
export type Commitments = z.infer<typeof commitments>
export type Index = 0 | 1
export const INDICES = [0, 1] as const

function uuidField(id: string) {
  return Field(BigInt(`0x${id.replaceAll('-', '')}`))
}

export function contextFor({ job, epoch }: Assignment) {
  return Poseidon.hash([Field(1), uuidField(job), Field(epoch)])
}

export const domain = { token: Field(2), activation: Field(3), cache: Field(4) }

export function commit(kind: Field, context: Field, slot: Field, values: Field[], blind: Field) {
  return Poseidon.hash([kind, context, slot, blind, ...values])
}

export function tokenCommitment(context: Field, position: number, opening: Opening) {
  return commit(domain.token, context, Field(position), [Field(opening.token)], Field(opening.blind)).toString()
}

export function statementDigest(
  context: Field, position: Field, stage: Field, lease: Field,
  input: Field, output: Field, cacheBefore: Field, cacheAfter: Field,
) {
  return Poseidon.hash([Field(5), context, position, stage, lease, input, output, cacheBefore, cacheAfter])
}

export function stageDigest(assignment: Assignment, position: Index, stage: Index, claims: Commitments) {
  return statementDigest(contextFor(assignment), Field(position), Field(stage),
    uuidField(assignment.leases[position][stage]), Field(claims.input), Field(claims.output),
    Field(claims.cacheBefore), Field(claims.cacheAfter))
}

export function stageLease(assignment: Assignment, position: Index, stage: Index) {
  return uuidField(assignment.leases[position][stage])
}

/** The assignment comes from the requester/coordinator, independently of the receipt. */
export async function verifyReceipt(assignmentValue: unknown, receiptValue: unknown, replyValue?: unknown) {
  const assignment = assignmentSchema.safeParse(assignmentValue)
  const receipt = receiptSchema.safeParse(receiptValue)
  if (!assignment.success || !receipt.success) return false

  const [[first, firstHead], [second, secondHead]] = receipt.data.steps
  if (first.input !== assignment.data.input
    || first.output !== firstHead.input || second.output !== secondHead.input
    || firstHead.output !== second.input || first.cacheAfter !== second.cacheBefore
    || firstHead.cacheAfter !== secondHead.cacheBefore) return false

  if (replyValue !== undefined) {
    const reply = replySchema.safeParse(replyValue)
    if (!reply.success) return false
    const context = contextFor(assignment.data)
    if (firstHead.output !== tokenCommitment(context, 1, reply.data[0])
      || secondHead.output !== tokenCommitment(context, 2, reply.data[1])) return false
  }

  for (const position of INDICES) {
    for (const stage of INDICES) {
      const claims = receipt.data.steps[position][stage]
      const publicInput = [stageDigest(assignment.data, position, stage, claims).toString()]
      try {
        if (!await verify({ proof: claims.proof, publicInput, publicOutput: [], maxProofsVerified: 0 },
          verificationKey.data)) return false
      } catch {
        return false
      }
    }
  }
  return true
}
