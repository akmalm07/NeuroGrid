import { Activity, ArrowRight, BadgeCheck, CircuitBoard, Coins, Cpu, Layers3 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useNetwork } from '../neurogrid/live'
import { MODEL } from '../neurogrid/protocol'
import { Coverage, networkReady, Shell, StartLinks } from '../neurogrid/ui'

const process = [
  { icon: CircuitBoard, title: 'Run one partition', text: 'The desktop app loads ten transformer layers and uses the CPU budget you set.' },
  { icon: Layers3, title: 'Generate together', text: 'Each shard passes its output to the next. The coordinator repeats the sequence for every new token.' },
  { icon: BadgeCheck, title: 'Get credit for work', text: 'A trusted reference checks every result. Successful requests pay contributors from the inference fee.' },
]

export default function Home() {
  const { result, failed } = useNetwork()
  const online = result?.nodes.filter(({ status }) => status === 'online' || status === 'working').length
  return <Shell>
    <section className="grid-hero" data-testid="static-landing">
      <div className="hero-art" aria-hidden="true" />
      <div className="grid-container hero-inner">
        <h1>One model.<br /><em>Many machines.</em></h1>
        <p>NeuroGrid splits a language model across volunteer computers. Try the chat or host a piece of the model and earn compute credits for verified work.</p>
        <StartLinks />
      </div>
    </section>
    <section className="home-metrics" aria-label="Network and model at a glance">
      <div className="grid-container metrics">
        <div><Cpu size={20} /><div><strong>{online ?? '—'}</strong><span>Nodes online</span>
          <Link to="/network" className="metric-status">
            <span className={'live-dot ' + (networkReady(result) ? 'available' : '')} />
            {failed ? 'Status unavailable' : !result ? 'Checking network' : networkReady(result) ? 'Network ready' : 'Waiting for capacity'}
          </Link>
        </div></div>
        <div><Layers3 size={20} /><div><strong>30</strong><span>Transformer layers</span></div></div>
        <div><Activity size={20} /><div><strong>{result?.settled ?? '—'}</strong><span>Verified requests</span></div></div>
        <div><Coins size={20} /><div><strong>100,000</strong><span>Simulated credit supply</span></div></div>
      </div>
    </section>
    <section className="grid-container home-section">
      <div className="section-heading">
        <div><h2>Follow the signal</h2><p>Three partitions run {MODEL}, ten layers at a time.</p></div>
        <Link className="text-link" to="/network">View the network <ArrowRight size={17} /></Link>
      </div>
      <Coverage network={result} />
    </section>
    <section className="workflow">
      <div className="grid-container home-section">
        <div className="section-heading"><div><h2>How it works</h2><p>From your computer to a verified response.</p></div></div>
        <div className="workflow-grid">{process.map(({ icon: Icon, title, text }) => <article key={title}>
          <Icon size={24} /><h3>{title}</h3><p>{text}</p>
        </article>)}</div>
        <p className="workflow-note">The coordinator verifies outputs and records settlements in a SHA-256 ledger. Credits are for the demo and have no monetary value. <Link to="/ledger">Explore the ledger <ArrowRight size={16} /></Link></p>
      </div>
    </section>
  </Shell>
}

