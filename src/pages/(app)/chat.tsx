import { ArrowUp, Bot, CircleAlert, Coins } from 'lucide-react'
import { useAuth } from 'deepspace'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import { Button } from '@/components/ui/Button'
import { explain, messages, readAccount, submit, type Account } from '../../neurogrid/api'
import { useGridRevision, useNetwork } from '../../neurogrid/live'
import { MODEL, SHARDS, fee } from '../../neurogrid/protocol'
import { networkReady, PageHeading } from '../../neurogrid/ui'

const suggestions = ['What is a neural network?', 'Why do bees matter?', 'Describe a solar eclipse.']

export default function Chat() {
  const { isSignedIn, userId } = useAuth()
  const showSignIn = useOutletContext<(show: boolean) => void>()
  const { result: network } = useNetwork(useGridRevision())
  const [account, setAccount] = useState<{ owner: string; data: Account }>()
  const [prompt, setPrompt] = useState('')
  const [limit, setLimit] = useState(16)
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const requestKey = useRef('')
  const conversation = useRef<HTMLDivElement>(null)
  const data = account && account.owner === userId ? account.data : undefined
  const pending = data?.jobs.find((job) => job.status !== 'settled' && job.status !== 'refunded')
  const ready = networkReady(network)

  useEffect(() => {
    if (!isSignedIn || !userId) return
    const owner = userId
    let current = true
    let loading = false
    async function refresh() {
      if (loading) return
      loading = true
      try {
        const result = await readAccount()
        if (current) setAccount({ owner, data: result })
      } catch (reason) {
        if (current) setError(explain(reason))
      } finally { loading = false }
    }
    void refresh()
    const timer = setInterval(refresh, pending ? 1_100 : 6_000)
    return () => { current = false; clearInterval(timer) }
  }, [isSignedIn, userId, pending?.id])

  useEffect(() => {
    if (conversation.current) conversation.current.scrollTop = conversation.current.scrollHeight
  }, [data?.jobs[0]?.id, data?.jobs[0]?.status])

  function updatePrompt(value: string) {
    setPrompt(value)
    requestKey.current = ''
    setError('')
  }

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (sending || pending || !prompt.trim() || !userId || !ready || !data) return
    setSending(true)
    setError('')
    requestKey.current ||= crypto.randomUUID()
    try {
      const job = await submit(prompt, limit, requestKey.current)
      updatePrompt('')
      setAccount((previous) => previous?.owner === userId
        ? { owner: userId, data: { ...previous.data,
          jobs: [job, ...previous.data.jobs.filter((item) => item.id !== job.id)] } } : previous)
      const latest = await readAccount()
      setAccount({ owner: userId, data: latest })
    } catch (reason) {
      setError(explain(reason))
    } finally { setSending(false) }
  }

  return <div className="grid-container page-body">
    <PageHeading title="Chat">Ask a short question. Three CPU partitions generate the answer one token at a time.</PageHeading>
    <div className="chat-grid">
      <section className="surface chat-panel" aria-label="Conversation">
        <div className="chat-title"><Bot size={21} /><strong>{MODEL}</strong><span className={'live-dot ' + (ready ? 'available' : '')} /></div>
        <div ref={conversation} className="conversation" role="log" aria-label="Chat history">
          {!data?.jobs.length && <div className="chat-welcome">
            <Bot size={36} /><h2>What would you like to ask?</h2>
            <p>Each prompt starts a fresh context.</p>
            <div className="suggestions">{suggestions.map((item) =>
              <button className="grid-button" key={item} onClick={() => updatePrompt(item)}>{item}</button>)}</div>
          </div>}
          {data?.jobs.slice().reverse().map((job) => <div className="chat-turn" key={job.id}>
            <p className="user-message">{job.prompt}</p>
            <div className="assistant-message">
              <Bot size={20} aria-label="NeuroGrid" />
              {job.status === 'settled' ? <div><p>{job.output || 'The model ended without a text response.'}</p>
                <small>{job.count} tokens · {job.charge} credits</small></div>
                : job.status === 'refunded' ? <p>{messages[job.reason] ?? 'Request refunded.'}</p>
                  : <p>{job.status === 'prepare' ? 'Preparing the prompt' : 'Partition ' + (job.stage + 1)
                    + ' · ' + (job.status === 'verifying' ? 'Verifying' : job.status === 'working' ? 'Computing' : 'Waiting')}
                    {' · '}{job.count}/{job.maxTokens} tokens</p>}
            </div>
          </div>)}
        </div>
        <form className="composer" onSubmit={send}>
          {error && <p className="form-error" role="alert">{error}</p>}
          <label className="sr-only" htmlFor="prompt">Message</label>
          <textarea id="prompt" value={prompt} onChange={(event) => updatePrompt(event.target.value)}
            placeholder="Ask NeuroGrid…" rows={2} maxLength={500}
            onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault()
              event.currentTarget.form?.requestSubmit()
            } }} />
          <div className="composer-bottom">
            <label>Maximum tokens <select value={limit} onChange={(event) => { setLimit(Number(event.target.value)); requestKey.current = '' }}>
              {[4, 8, 16, 24].map((size) => <option key={size}>{size}</option>)}
            </select></label>
            {isSignedIn ? <Button type="submit" className="grid-action" aria-label="Send prompt"
              disabled={!prompt.trim() || !ready || Boolean(pending) || sending || !data}><ArrowUp size={18} />Send</Button>
              : <Button type="button" className="grid-action" onClick={() => showSignIn(true)}>Sign in to chat</Button>}
          </div>
        </form>
      </section>
      <aside className="chat-sidebar">
        <section className="surface side-panel">
          <Coins size={21} /><h2>Compute credits</h2>
          <strong className="large-value">{data?.credits ?? '—'}</strong>
          <p>Up to {fee(limit)} credits reserved. Unused credits come back to your balance.</p>
        </section>
        <section className="surface side-panel">
          <h2>Inference pipeline</h2>
          <ol className="pipeline">
            {SHARDS.map((shard, index) => <li key={shard.title} className={pending?.stage === index ? 'current' : ''}>
              <span>{index + 1}</span><div>{shard.title}<small>Layers {shard.layers}</small></div>
            </li>)}
          </ol>
          {!ready && <p className="capacity"><CircleAlert size={16} />Waiting for the verifier and all three partitions.</p>}
          <Link className="text-link" to="/network">Network status</Link>
        </section>
      </aside>
    </div>
  </div>
}

