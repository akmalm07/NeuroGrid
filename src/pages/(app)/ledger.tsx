import { Download, ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { explain, readLedger } from '../../neurogrid/api'
import { verifyChain } from '../../neurogrid/ledger'
import { useGridRevision, useNetwork } from '../../neurogrid/live'
import { SUPPLY, type Block } from '../../neurogrid/protocol'
import { PageHeading } from '../../neurogrid/ui'

const eventName = { genesis: 'Genesis', grant: 'Account grant', reserve: 'Inference reservation', settle: 'Verified settlement',
  refund: 'Refund', checkout: 'Checkout reservation', purchase: 'Credit purchase', checkout_release: 'Checkout released' }

export default function Ledger() {
  const { result } = useNetwork(useGridRevision())
  const [blocks, setBlocks] = useState<Block[]>()
  const [error, setError] = useState('')
  const [outcome, setOutcome] = useState<{ message: string; head: string; valid: boolean }>()
  const [validating, setValidating] = useState(false)
  const [shown, setShown] = useState(20)
  const height = result?.height

  useEffect(() => {
    let current = true
    readLedger().then((data) => {
      if (current) { setBlocks(data); setError('') }
    }).catch((reason) => { if (current) setError(explain(reason)) })
    return () => { current = false }
  }, [height])

  async function verify() {
    if (!blocks) return
    const head = blocks.at(-1)?.hash ?? ''
    setValidating(true)
    try {
      const check = await verifyChain(blocks)
      setOutcome({ message: 'Verified ' + (check.height + 1) + ' blocks from genesis.', head, valid: true })
    } catch (reason) {
      setOutcome({ message: reason instanceof Error ? reason.message : 'Verification failed', head, valid: false })
    } finally { setValidating(false) }
  }

  function save() {
    const url = URL.createObjectURL(new Blob([JSON.stringify(blocks, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'neurogrid-ledger.json'
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1_000)
  }

  const head = blocks?.at(-1)
  return <div className="grid-container page-body">
    <PageHeading title="Ledger">Follow credits from the treasury to a request, then to its workers or back to the requester.</PageHeading>
    <section className="surface ledger-summary">
      <div><h2>Chain head</h2><code>{head?.hash ?? '—'}</code></div>
      <div><h2>Total supply</h2><strong>{SUPPLY.toLocaleString()} credits</strong></div>
      <div className="ledger-actions">
        <Button className="grid-action" disabled={!blocks || validating} onClick={() => void verify()}><ShieldCheck size={17} />Verify ledger</Button>
        <Button variant="outline" disabled={!blocks} onClick={save}><Download size={17} />Export JSON</Button>
      </div>
      {outcome && outcome.head === head?.hash && <p role="status"
        className={outcome.valid ? 'verification-ok' : 'form-error'}>{outcome.message}</p>}
    </section>
    <section className="surface registry network-section">
      <h2>Blocks</h2>
      {error && <p className="form-error" role="alert">{error}</p>}
      {blocks ? <>
        <div className="block-list">{blocks.slice().reverse().slice(0, shown).map((block) => <details key={block.hash}>
          <summary><span>#{block.height}</span><strong>{eventName[block.kind]}</strong>
            <time dateTime={new Date(block.createdAt).toISOString()}>{new Date(block.createdAt).toLocaleString()}</time></summary>
          <dl className="block-detail">
            <dt>Hash</dt><dd>{block.hash}</dd>
            <dt>Previous hash</dt><dd>{block.previousHash}</dd>
            <dt>Reference</dt><dd>{block.reference}</dd>
            {block.proof && <><dt>Receipt digest</dt><dd>{block.proof}</dd></>}
          </dl>
          <ul className="transfers">{block.transfers.map(([id, delta]) => <li key={id}><code>{id}</code>
            <span className={delta > 0 ? 'verification-ok' : ''}>{delta > 0 ? '+' : ''}{delta.toLocaleString()}</span></li>)}</ul>
        </details>)}</div>
        {blocks.length > shown && <Button variant="outline" onClick={() => setShown(shown + 20)}>Show older blocks</Button>}
      </> : <p>Loading the block history…</p>}
    </section>
    <p className="page-note">The trusted coordinator writes the ledger. SHA-256 links let you detect a changed record when you have a known chain head.</p>
  </div>
}

