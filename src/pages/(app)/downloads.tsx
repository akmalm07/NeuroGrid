import { ArrowDownToLine, Copy, KeyRound, Monitor, Terminal } from 'lucide-react'
import { useAuth } from 'deepspace'
import { useEffect, useState } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import { Button } from '@/components/ui/Button'
import { enroll, explain, readAccount, revoke, type Account, type Enrollment } from '../../neurogrid/api'
import { SHARDS } from '../../neurogrid/protocol'
import { PageHeading } from '../../neurogrid/ui'

const release = 'https://github.com/akmalm07/NeuroGrid/releases/latest/download/'
const platforms = [
  { name: 'Windows', icon: Monitor, asset: 'NeuroGrid-Node-Windows-x64.zip',
    system: 'Windows 10 or 11 · x86-64 · Python 3.12',
    steps: ['Extract the ZIP to a folder you can keep.', 'Open PowerShell in that folder and run the installer.', 'Open Start NeuroGrid.cmd after installation.'],
    command: 'powershell -NoProfile -ExecutionPolicy Bypass -File .\\Install-NeuroGrid.ps1' },
  { name: 'Linux', icon: Terminal, asset: 'NeuroGrid-Node-Linux-x64.tar.gz',
    system: 'Ubuntu 24.04 or compatible desktop · x86-64 · Python 3.12 with venv',
    steps: ['Extract the archive to a folder you can keep.', 'Open a terminal in that folder and run the installer.', 'Run ./start-neurogrid from the same folder.'],
    command: './install-neurogrid.sh' },
]

export default function Downloads() {
  const { isSignedIn, userId } = useAuth()
  const showSignIn = useOutletContext<(show: boolean) => void>()
  const [account, setAccount] = useState<{ owner: string; nodes: Account['nodes'] }>()
  const [key, setKey] = useState<{ owner: string; enrollment: Enrollment }>()
  const [shard, setShard] = useState('auto')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const issued = key?.owner === userId ? key?.enrollment : undefined

  useEffect(() => {
    if (!isSignedIn || !userId) return
    let current = true
    readAccount().then((result) => { if (current) setAccount({ owner: userId, nodes: result.nodes }) })
      .catch((reason) => { if (current) setError(explain(reason)) })
    return () => { current = false }
  }, [isSignedIn, userId])

  async function change(id?: string) {
    if (!userId || busy) return
    setBusy(true)
    setCopied(false)
    setError('')
    try {
      if (id) {
        await revoke(id)
        if (issued?.id === id) setKey(undefined)
      } else {
        setKey({ owner: userId, enrollment: await enroll(shard) })
      }
      setAccount({ owner: userId, nodes: (await readAccount()).nodes })
    } catch (reason) { setError(explain(reason)) }
    finally { setBusy(false) }
  }

  return <div className="grid-container page-body">
    <PageHeading title="Start computing">Install the desktop node, pair it with your account, then choose how much CPU time to contribute.</PageHeading>
    <div className="download-grid">{platforms.map(({ name, icon: Icon, asset, system, steps, command }) =>
      <section key={name} className="surface download-card">
        <Icon size={29} /><h2>{name}</h2><p>{system}</p>
        <a href={release + asset} className="grid-button filled"><ArrowDownToLine size={17} />Download for {name}</a>
        <ol>{steps.map((step) => <li key={step}>{step}</li>)}</ol>
        <pre className="command"><code>{command}</code></pre>
      </section>)}</div>
    <p className="page-note">Allow 4 GB of RAM and 4 GB of free disk space. Installation downloads the CPU runtime and the first launch downloads the pinned model checkpoint. A GPU is optional.</p>
    <section className="surface download-card network-section" aria-labelledby="pair-title">
      <KeyRound size={26} /><h2 id="pair-title">Pair a compute node</h2>
      <p>Each key connects one installation. In the desktop app, enter this site address and the key, then select Start.</p>
      <pre className="command"><code>{window.location.origin}</code></pre>
      {!isSignedIn ? <Button className="grid-action" onClick={() => showSignIn(true)}>Sign in to create a key</Button>
        : <div className="key-controls">
          <label>Partition <select value={shard} onChange={(event) => setShard(event.target.value)}>
            <option value="auto">Assign automatically</option>
            {SHARDS.map((part, index) => <option key={part.title} value={index}>{part.title} · {part.layers}</option>)}
          </select></label>
          <Button className="grid-action" disabled={busy} onClick={() => void change()}>Create node key</Button>
        </div>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {issued && isSignedIn && <div className="issued-key">
        <p>Copy this key now. It allows this node to claim work for 30 days.</p>
        <label className="sr-only" htmlFor="node-key">Node key</label>
        <input readOnly id="node-key" value={issued.secret} onFocus={(event) => event.target.select()} />
        <Button variant="outline" onClick={() => void navigator.clipboard.writeText(issued.secret)
          .then(() => setCopied(true), () => setError('Select and copy the key manually.'))}><Copy size={16} />{copied ? 'Copied' : 'Copy key'}</Button>
      </div>}
      {isSignedIn && account?.owner === userId && Boolean(account?.nodes.length) && <ul className="node-keys">
        {account?.nodes.map((node) => <li key={node.id}>
          <span><code>{node.id.split('-')[0]}</code> · {SHARDS[node.shard].title}</span>
          <Button variant="outline" size="sm" disabled={busy} onClick={() => void change(node.id)}>Revoke key</Button>
        </li>)}
      </ul>}
      <p className="pair-footer">You can pause or quit from the tray. Launch at sign-in is opt-in. <Link to="/network">See registered nodes</Link></p>
    </section>
  </div>
}

