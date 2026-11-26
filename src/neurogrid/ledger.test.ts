import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { EMPTY_HASH, verifyChain } from './ledger'
import { blockBytes, type Block, type BlockData } from './protocol'

function seal(previous: Block | undefined, kind: Block['kind'], reference: string, transfers: Block['transfers']): Block {
  const block: BlockData = {
    version: kind === 'units' || previous?.version === 2 ? 2 : 1,
    height: previous ? previous.height + 1 : 0, previousHash: previous?.hash ?? EMPTY_HASH,
    createdAt: 1_000, kind, reference, transfers,
    proof: kind === 'purchase_adjustment' ? reference.split(':')[1]
      : kind === 'settle' || kind === 'purchase' ? 'a'.repeat(64) : '',
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

  it('rejects a balanced settlement that debits an account outside escrow', async () => {
    const extraDebit = seal(reserve, 'settle', 'job-id', [
      ['held:job-id', -7], ['treasury', 1], ['acct:requester', -5], ['acct:worker', 11],
    ])
    await expect(verifyChain([genesis, grant, reserve, extraDebit])).rejects.toThrow('block 3')
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

  it('replays the migration, credit issuance, reward, platform retirement, and a Stripe reversal', async () => {
    const units = seal(grant, 'units', 'granite-credit-subunits-v2', [])
    const checkout = seal(units, 'checkout', 'order', [])
    const purchase = seal(checkout, 'purchase', 'order', [['acct:buyer', 100_000]])
    const hold = seal(purchase, 'reserve', 'job', [['acct:buyer', -2_000], ['held:job', 2_000]])
    const settled = seal(hold, 'settle', 'job', [['held:job', -2_000], ['acct:worker', 1_400]])
    const reversed = seal(settled, 'purchase_adjustment', `order:${'b'.repeat(64)}`, [['acct:buyer', -100_000]])
    const chain = [genesis, grant, units, checkout, purchase, hold, settled, reversed]
    await expect(verifyChain(chain)).resolves.toMatchObject({ height: 7 })
    const overRefund = seal(reversed, 'purchase_adjustment', `order:${'c'.repeat(64)}`, [['acct:buyer', -1_000]])
    await expect(verifyChain([...chain, overRefund])).rejects.toThrow('block 8')
  })

  it('scales an open legacy Checkout escrow without changing its owner or amount', async () => {
    const checkout = seal(grant, 'checkout', 'old-order', [['treasury', -500], ['checkout:old-order', 500]])
    const units = seal(checkout, 'units', 'granite-credit-subunits-v2', [])
    const purchase = seal(units, 'purchase', 'old-order', [['checkout:old-order', -500_000], ['acct:buyer', 500_000]])
    await expect(verifyChain([genesis, grant, checkout, units, purchase])).resolves.toMatchObject({ height: 4 })
  })

  it('replays fractional metering, a concurrent hold, and a complete refund', async () => {
    const units = seal(grant, 'units', 'granite-credit-subunits-v2', [])
    const first = seal(units, 'reserve', 'first', [['acct:requester', -590], ['held:first', 590]])
    const second = seal(first, 'reserve', 'second', [['acct:requester', -828], ['held:second', 828]])
    const settled = seal(second, 'settle', 'first', [
      ['held:first', -590], ['acct:requester', 403], ['acct:worker', 130],
    ])
    const refund = seal(settled, 'refund', 'second', [['held:second', -828], ['acct:requester', 828]])
    await expect(verifyChain([genesis, grant, units, first, second, settled, refund])).resolves.toMatchObject({ height: 6 })
    const duplicate = seal(refund, 'settle', 'first', settled.transfers)
    await expect(verifyChain([genesis, grant, units, first, second, settled, refund, duplicate])).rejects.toThrow('block 7')
  })
})
