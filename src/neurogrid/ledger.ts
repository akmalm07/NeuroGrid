import { GRANT, SUPPLY, UNIT, blockBytes, type Block, type BlockKind } from './protocol'

export const EMPTY_HASH = '0'.repeat(64)
export const TREASURY = 'treasury'
const KINDS: BlockKind[] = ['genesis', 'grant', 'reserve', 'settle', 'refund',
  'checkout', 'purchase', 'checkout_release', 'units', 'purchase_adjustment']
const encoder = new TextEncoder()

export async function verifyChain(blocks: Block[]): Promise<{ height: number; head: string }> {
  let previous = EMPTY_HASH
  const balances = new Map<string, number>()
  const reservations = new Map<string, { account: string; amount: number }>()
  const checkouts = new Map<string, number>()
  const paidReceipts = new Set<string>()
  const purchases = new Map<string, { owner: string; issued: number; reversed: number }>()
  const seen = new Set<string>()
  let unit = 1
  for (const [height, block] of blocks.entries()) {
    const invalid = () => new Error(`Invalid ledger at block ${height}`)
    const digest = await crypto.subtle.digest('SHA-256', encoder.encode(blockBytes(block)))
    const hash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
    const key = `${block.kind}/${block.reference}`
    if (block.version !== (unit === UNIT || block.kind === 'units' ? 2 : 1)
        || block.height !== height || block.previousHash !== previous
        || hash !== block.hash || !KINDS.includes(block.kind) || seen.has(key)) throw invalid()
    seen.add(key)
    const transfers = new Map(block.transfers)
    if (transfers.size !== block.transfers.length) throw invalid()
    const total = block.transfers.reduce((sum, [, delta]) => sum + delta, 0)
    if (height === 0) {
      if (block.kind !== 'genesis' || total !== SUPPLY || transfers.get(TREASURY) !== SUPPLY
          || transfers.size !== 1) throw invalid()
    } else if (block.kind === 'units') {
      if (unit !== 1 || block.reference !== 'granite-credit-subunits-v2' || transfers.size || reservations.size) throw invalid()
      unit = UNIT
      for (const [name, balance] of balances) balances.set(name, balance * UNIT)
      for (const [name, amount] of checkouts) checkouts.set(name, amount * UNIT)
      for (const payment of purchases.values()) payment.issued *= UNIT
    } else if (block.kind === 'genesis' || total !== 0
        && !(block.version === 2 && ['settle', 'purchase', 'purchase_adjustment'].includes(block.kind))) {
      throw invalid()
    }
    const escrow = `held:${block.reference}`
    const checkout = `checkout:${block.reference}`
    if (block.kind === 'checkout') {
      const amount = transfers.get(checkout) ?? 0
      if (block.version === 2 ? transfers.size !== 0
        : amount <= 0 || transfers.size !== 2 || transfers.get(TREASURY) !== -amount) throw invalid()
      checkouts.set(block.reference, amount)
    }
    if (block.kind === 'purchase' || block.kind === 'checkout_release') {
      const amount = checkouts.get(block.reference)
      const recipient = block.transfers.find(([name, delta]) => delta === amount &&
        (block.kind === 'purchase' ? name.startsWith('acct:') : name === TREASURY))
      if (amount === undefined) throw invalid()
      if (amount > 0) {
        if (!recipient || transfers.size !== 2 || transfers.get(checkout) !== -amount || total !== 0) throw invalid()
      } else if (block.version !== 2 || (block.kind === 'purchase'
        ? transfers.size !== 1 || !block.transfers[0]?.[0].startsWith('acct:') || total <= 0 || total % UNIT !== 0
        : transfers.size !== 0)) throw invalid()
      if (block.kind === 'purchase') {
        if (!/^[\da-f]{64}$/.test(block.proof) || paidReceipts.has(block.proof)) throw invalid()
        paidReceipts.add(block.proof)
        const owner = block.transfers.find(([name, delta]) => name.startsWith('acct:') && delta > 0)
        if (!owner) throw invalid()
        purchases.set(block.reference, { owner: owner[0], issued: owner[1], reversed: 0 })
      }
      checkouts.delete(block.reference)
    }
    if (block.kind === 'purchase_adjustment') {
      const [order, digest, extra] = block.reference.split(':')
      const purchase = purchases.get(order)
      if (block.version !== 2 || !purchase || extra !== undefined || digest !== block.proof
          || !/^[\da-f]{64}$/.test(block.proof) || transfers.size !== 1 || !total || total % UNIT !== 0
          || transfers.get(purchase.owner) !== total) throw invalid()
      purchase.reversed -= total
      if (purchase.reversed < 0 || purchase.reversed > purchase.issued) throw invalid()
    }
    if (block.kind === 'grant') {
      if (!block.reference.startsWith('acct:') || transfers.get(block.reference) !== GRANT * unit
          || transfers.get(TREASURY) !== -GRANT * unit || transfers.size !== 2) throw invalid()
    }
    if (block.kind === 'reserve') {
      const amount = transfers.get(escrow) ?? 0
      const owner = block.transfers.find(([name, delta]) => name.startsWith('acct:') && delta === -amount)
      if (amount <= 0 || !owner || transfers.size !== 2) throw invalid()
      reservations.set(block.reference, { account: owner[0], amount })
    }
    if (block.kind === 'settle' || block.kind === 'refund') {
      const held = reservations.get(block.reference)
      if (!held || transfers.get(escrow) !== -held.amount) throw invalid()
      if (block.kind === 'refund' && (transfers.size !== 2 || transfers.get(held.account) !== held.amount)) throw invalid()
      if (block.kind === 'settle') {
        if (!/^[\da-f]{64}$/.test(block.proof) || block.transfers.some(([name, delta]) =>
          name !== escrow && (block.version !== 1 || name !== TREASURY) && (!name.startsWith('acct:') || delta <= 0))) {
          throw invalid()
        }
        if (block.version === 1 ? transfers.get(TREASURY) !== 1 : total >= 0) throw invalid()
      }
      reservations.delete(block.reference)
    }
    for (const [name, delta] of block.transfers) {
      if (!Number.isSafeInteger(delta) || typeof name !== 'string') throw invalid()
      const previousBalance = balances.get(name) ?? 0
      const balance = previousBalance + delta
      const minimum = block.kind === 'purchase_adjustment' ? Number.MIN_SAFE_INTEGER : Math.min(previousBalance, 0)
      if (!Number.isSafeInteger(balance) || balance < minimum || (block.version === 1 && balance > SUPPLY)) throw invalid()
      balances.set(name, balance)
    }
    previous = hash
  }
  if (!blocks.length) throw new Error('Ledger is empty')
  return { height: blocks.length - 1, head: previous }
}
