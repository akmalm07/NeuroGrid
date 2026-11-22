import { GRANT, SUPPLY, blockBytes, type Block, type BlockKind } from './protocol'

export const EMPTY_HASH = '0'.repeat(64)
export const TREASURY = 'treasury'
const KINDS: BlockKind[] = ['genesis', 'grant', 'reserve', 'settle', 'refund',
  'checkout', 'purchase', 'checkout_release']
const encoder = new TextEncoder()

export async function verifyChain(blocks: Block[]): Promise<{ height: number; head: string }> {
  let previous = EMPTY_HASH
  const balances = new Map<string, number>()
  const reservations = new Map<string, { account: string; amount: number }>()
  const checkouts = new Map<string, number>()
  const paidReceipts = new Set<string>()
  const seen = new Set<string>()
  for (const [height, block] of blocks.entries()) {
    const invalid = () => new Error(`Invalid ledger at block ${height}`)
    const digest = await crypto.subtle.digest('SHA-256', encoder.encode(blockBytes(block)))
    const hash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
    const key = `${block.kind}/${block.reference}`
    if (block.version !== 1 || block.height !== height || block.previousHash !== previous
        || hash !== block.hash || !KINDS.includes(block.kind) || seen.has(key)) throw invalid()
    seen.add(key)
    const transfers = new Map(block.transfers)
    if (transfers.size !== block.transfers.length) throw invalid()
    const total = block.transfers.reduce((sum, [, delta]) => sum + delta, 0)
    if (height === 0) {
      if (block.kind !== 'genesis' || total !== SUPPLY || transfers.get(TREASURY) !== SUPPLY
          || transfers.size !== 1) throw invalid()
    } else if (total !== 0 || block.kind === 'genesis') {
      throw invalid()
    }
    const escrow = `held:${block.reference}`
    const checkout = `checkout:${block.reference}`
    if (block.kind === 'checkout') {
      const amount = transfers.get(checkout) ?? 0
      if (amount <= 0 || transfers.size !== 2 || transfers.get(TREASURY) !== -amount) throw invalid()
      checkouts.set(block.reference, amount)
    }
    if (block.kind === 'purchase' || block.kind === 'checkout_release') {
      const amount = checkouts.get(block.reference)
      const recipient = block.transfers.find(([name, delta]) => delta === amount &&
        (block.kind === 'purchase' ? name.startsWith('acct:') : name === TREASURY))
      if (!amount || !recipient || transfers.size !== 2 || transfers.get(checkout) !== -amount) throw invalid()
      if (block.kind === 'purchase') {
        if (!/^[\da-f]{64}$/.test(block.proof) || paidReceipts.has(block.proof)) throw invalid()
        paidReceipts.add(block.proof)
      }
      checkouts.delete(block.reference)
    }
    if (block.kind === 'grant') {
      if (!block.reference.startsWith('acct:') || transfers.get(block.reference) !== GRANT
          || transfers.get(TREASURY) !== -GRANT || transfers.size !== 2) throw invalid()
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
      if (block.kind === 'settle' && (!/^[\da-f]{64}$/.test(block.proof) || transfers.get(TREASURY) !== 1
          || block.transfers.some(([name, delta]) =>
            name !== escrow && name !== TREASURY && (!name.startsWith('acct:') || delta <= 0)))) throw invalid()
      reservations.delete(block.reference)
    }
    for (const [name, delta] of block.transfers) {
      if (!Number.isSafeInteger(delta) || typeof name !== 'string') throw invalid()
      const balance = (balances.get(name) ?? 0) + delta
      if (balance < 0 || balance > SUPPLY) throw invalid()
      balances.set(name, balance)
    }
    previous = hash
  }
  if (!blocks.length) throw new Error('Ledger is empty')
  return { height: blocks.length - 1, head: previous }
}
