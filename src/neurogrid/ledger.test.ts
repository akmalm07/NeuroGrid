import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { EMPTY_HASH, verifyChain } from './ledger'
import { blockBytes, type Block, type BlockData } from './protocol'

function seal(previous: Block | undefined, kind: Block['kind'], reference: string, transfers: Block['transfers']): Block {
  const block: BlockData = {
    version: 1, height: previous ? previous.height + 1 : 0, previousHash: previous?.hash ?? EMPTY_HASH,
    createdAt: 1_000, kind, reference, transfers,
    proof: kind === 'settle' || kind === 'purchase' ? 'a'.repeat(64) : '',
  }
  return { ...block, hash: createHash('sha256').update(blockBytes(block)).digest('hex') }
}

const genesis = seal(undefined, 'genesis', 'neurogrid-v1', [['treasury', 100_000]])
const grant = seal(genesis, 'grant', 'acct:requester', [['treasury', -100], ['acct:requester', 100]])
const reserve = seal(grant, 'reserve', 'job-id', [['acct:requester', -7], ['held:job-id', 7]])
const settle = seal(reserve, 'settle', 'job-id', [
  ['held:job-id', -7], ['treasury', 1], ['acct:requester', 3], ['acct:worker', 3],
])

describe('ledger replay', () => {
  it('verifies a valid settlement', async () => {
    await expect(verifyChain([genesis, grant, reserve, settle])).resolves.toEqual({ height: 3, head: settle.hash })
  })

  it('verifies a full refund', async () => {
    const refund = seal(reserve, 'refund', 'job-id', [['held:job-id', -7], ['acct:requester', 7]])
    await expect(verifyChain([genesis, grant, reserve, refund])).resolves.toMatchObject({ height: 3 })
  })

  it('rejects edits that preserve the transfer sum', async () => {
    const corrupted: Block = { ...grant, transfers: [['treasury', -99], ['acct:requester', 99]] }
    await expect(verifyChain([genesis, corrupted, reserve, settle])).rejects.toThrow('block 1')
  })

  it('rejects a correctly hashed second settlement', async () => {
    const duplicate = seal(settle, 'settle', 'job-id', settle.transfers)
    await expect(verifyChain([genesis, grant, reserve, settle, duplicate])).rejects.toThrow('block 4')
  })

  it('rejects a correctly hashed overspend', async () => {
    const overspend = seal(grant, 'reserve', 'job-id', [['acct:requester', -103], ['held:job-id', 103]])
    await expect(verifyChain([genesis, grant, overspend])).rejects.toThrow('block 2')
  })

  it('moves reserved checkout credits to the buyer once, preserving supply', async () => {
    const checkout = seal(grant, 'checkout', 'order', [['treasury', -500], ['checkout:order', 500]])
    const purchase = seal(checkout, 'purchase', 'order', [['checkout:order', -500], ['acct:buyer', 500]])
    const duplicate = seal(purchase, 'purchase', 'order', purchase.transfers)
    await expect(verifyChain([genesis, grant, checkout, purchase])).resolves.toMatchObject({ height: 3 })
    await expect(verifyChain([genesis, grant, checkout, purchase, duplicate])).rejects.toThrow('block 4')
  })

  it('rejects fulfillment after an expired checkout released the credits', async () => {
    const checkout = seal(grant, 'checkout', 'order', [['treasury', -500], ['checkout:order', 500]])
    const released = seal(checkout, 'checkout_release', 'order', [['checkout:order', -500], ['treasury', 500]])
    const purchase = seal(released, 'purchase', 'order', [['checkout:order', -500], ['acct:buyer', 500]])
    await expect(verifyChain([genesis, grant, checkout, released])).resolves.toMatchObject({ height: 3 })
    await expect(verifyChain([genesis, grant, checkout, released, purchase])).rejects.toThrow('block 4')
  })
})
