import { Field, Gadgets, Provable, Struct, UInt32, ZkProgram } from 'o1js'
import { MODEL, commit, domain, statementDigest, type Index } from './receipt.ts'

type Vector = [Field, Field]
type Matrix = readonly [readonly [number, number], readonly [number, number]]
export type Cache = [Field, Field, Field, Field]

const weights = [
  { query: [[2, 0], [0, 1]], key: [[1, 0], [1, 1]], value: [[1, 1], [0, 1]] },
  { query: [[1, 1], [0, 1]], key: [[1, 0], [0, 2]], value: [[1, 0], [1, 1]] },
] as const satisfies readonly { query: Matrix; key: Matrix; value: Matrix }[]
const embedding = [[1, 0], [0, 1], [2, 1], [1, 2]] as const

export class StageWitness extends Struct({
  context: Field,
  position: Field,
  lease: Field,
  input: Provable.Array(Field, 2),
  cache: Provable.Array(Field, 4),
  inputBlind: Field,
  outputBlind: Field,
  cacheBeforeBlind: Field,
  cacheAfterBlind: Field,
}) {}

export class StageClaims extends Struct({
  input: Field,
  output: Field,
  cacheBefore: Field,
  cacheAfter: Field,
}) {}

function dot([a, b]: Vector, [c, d]: Vector) {
  return a.mul(c).add(b.mul(d))
}

function project([[a, b], [c, d]]: Matrix, [x, y]: Vector): Vector {
  return [x.mul(a).add(y.mul(b)), x.mul(c).add(y.mul(d))]
}

function embed(token: Field): Vector {
  token.toBits(2)
  const selected = embedding.map((_, i) => token.equals(i).toField())
  return [
    selected.reduce((sum, bit, i) => sum.add(bit.mul(embedding[i][0])), Field(0)),
    selected.reduce((sum, bit, i) => sum.add(bit.mul(embedding[i][1])), Field(0)),
  ]
}

function greedy([x, y]: Vector) {
  const logits = [x.mul(2), y.mul(2), x.add(y).add(1), y.mul(3)]
  let best = logits[0]
  let token = Field(0)
  for (let i = 1; i < logits.length; i++) {
    // Each residual lane is at most 510, so every logit fits in 32 bits.
    const better = UInt32.Unsafe.fromField(best).lessThan(UInt32.Unsafe.fromField(logits[i]))
    best = Provable.if(better, logits[i], best)
    token = Provable.if(better, Field(i), token)
  }
  return token
}

export function runStage(stage: Index, witness: StageWitness) {
  const { context, position, cache, inputBlind, outputBlind, cacheBeforeBlind, cacheAfterBlind } = witness
  const [x, y] = witness.input
  const [oldK0, oldK1, oldV0, oldV1] = cache
  position.assertBool()
  for (const blind of [inputBlind, outputBlind, cacheBeforeBlind, cacheAfterBlind]) {
    blind.assertNotEquals(0)
  }
  const initial = position.equals(0)
  for (const value of cache) {
    Gadgets.rangeCheck8(value)
    initial.toField().mul(value).assertEquals(0)
  }
  const input: Vector = stage === 0 ? embed(x) : [x, y]
  if (stage === 0) y.assertEquals(0)
  for (const value of input) Gadgets.rangeCheck8(value)

  const params = weights[stage]
  const query = project(params.query, input)
  const key = project(params.key, input)
  const value = project(params.value, input)
  const nextCache: Cache = [...key, ...value]
  for (const lane of nextCache) Gadgets.rangeCheck8(lane)
  // Byte-bounded keys and two-lane queries give dot products below 2^19.
  const currentScore = UInt32.Unsafe.fromField(dot(query, key))
  const oldScore = UInt32.Unsafe.fromField(dot(query, [oldK0, oldK1]))
  const useOld = initial.not().and(currentScore.lessThan(oldScore))
  const activation: Vector = [
    input[0].add(Provable.if(useOld, oldV0, value[0])),
    input[1].add(Provable.if(useOld, oldV1, value[1])),
  ]
  if (stage === 0) for (const lane of activation) Gadgets.rangeCheck8(lane)

  const output = stage === 0 ? activation : [greedy(activation)]
  const inputCommitment = stage === 0
    ? commit(domain.token, context, position, [x], inputBlind)
    : commit(domain.activation, context, position, input, inputBlind)
  const outputCommitment = stage === 0
    ? commit(domain.activation, context, position, output, outputBlind)
    : commit(domain.token, context, position.add(1), output, outputBlind)
  const cacheSlot = position.add(stage * 3)
  const claims = new StageClaims({
    input: inputCommitment,
    output: outputCommitment,
    cacheBefore: commit(domain.cache, context, cacheSlot, cache, cacheBeforeBlind),
    cacheAfter: commit(domain.cache, context, cacheSlot.add(1), nextCache, cacheAfterBlind),
  })
  return { output, nextCache, claims }
}

function constrain(stage: Index, digest: Field, witness: StageWitness, claimed: StageClaims) {
  const { claims } = runStage(stage, witness)
  claims.input.assertEquals(claimed.input)
  claims.output.assertEquals(claimed.output)
  claims.cacheBefore.assertEquals(claimed.cacheBefore)
  claims.cacheAfter.assertEquals(claimed.cacheAfter)
  statementDigest(witness.context, witness.position, Field(stage), witness.lease,
    claimed.input, claimed.output, claimed.cacheBefore, claimed.cacheAfter).assertEquals(digest)
}

export const pilot = ZkProgram({
  name: MODEL,
  publicInput: Field,
  methods: {
    first: {
      privateInputs: [StageWitness, StageClaims],
      async method(digest, witness, claims) {
        constrain(0, digest, witness, claims)
      },
    },
    last: {
      privateInputs: [StageWitness, StageClaims],
      async method(digest, witness, claims) {
        constrain(1, digest, witness, claims)
      },
    },
  },
})
