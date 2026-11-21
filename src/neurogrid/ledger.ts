import { SUPPLY, blockBytes, type Block, type BlockKind } from './protocol'

export const EMPTY_HASH = '0'.repeat(64)
export const TREASURY = 'treasury'

export async function verifyChain(blocks: Block[]): Promise<{ height: number; head: string }> {
  let previous = EMPTY_HASH
  const balances = new Map<string, number>()
  const reservations = new Map<string, { account: string; amount: number }>()
  const seen = new Set<string>()
  for (const [height, block] of blocks.entries()) {
    const invalid = () => new Error(`Invalid ledger at block ${height}`)
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(blockBytes(block)))
    const hash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
    const kinds: BlockKind[] = ['genesis', 'grant', 'reserve', 'settle', 'refund']
    const key = `${block.kind}/${block.reference}`
    if (block.version !== 1 || block.height !== height || block.previousHash !== previous
        || hash !== block.hash || !kinds.includes(block.kind) || seen.has(key)) throw invalid()
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
    if (block.kind === 'grant') {
      const grant = transfers.get(block.reference) ?? 0
      if (grant <= 0 || transfers.get(TREASURY) !== -grant || transfers.size !== 2) throw invalid()
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
      if (block.kind === 'settle' && (!/^[\da-f]{64}$/.test(block.proof) || transfers.get(TREASURY) !== 1)) throw invalid()
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
