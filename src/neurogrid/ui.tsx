import { ArrowDownToLine, ArrowRight, Cable, Cpu, Hexagon, LogIn, LogOut } from 'lucide-react'
import { Link, NavLink } from 'react-router-dom'
import type { ReactNode } from 'react'
import { SHARDS, type NetworkSnapshot } from './protocol'

const pages = [['/', 'Home'], ['/chat', 'Chat'], ['/network', 'Network'], ['/ledger', 'Ledger'], ['/downloads', 'Downloads']]

export function Shell({ children, auth }: { children: ReactNode; auth?: { signedIn: boolean; action: () => void } }) {
  return (
    <div className="grid-site">
      <a className="skip" href="#content">Skip to content</a>
      <header className="grid-header">
        <div className="grid-container grid-nav" data-testid="app-navigation">
          <Link className="wordmark" to="/" aria-label="NeuroGrid home">
            <span className="brand-icon"><Hexagon aria-hidden="true" size={24} /><Cpu aria-hidden="true" size={13} /></span>
            Neuro<span>Grid</span>
          </Link>
          <nav aria-label="Main navigation">
            {pages.map(([path, name]) => <NavLink key={path} to={path} end={path === '/'}>{name}</NavLink>)}
          </nav>
          {auth ? (
            <button type="button" className="grid-button nav-signin" onClick={auth.action}
              data-testid={auth.signedIn ? 'nav-sign-out-button' : 'nav-sign-in-button'}>
              {auth.signedIn ? <LogOut size={16} /> : <LogIn size={16} />}
              {auth.signedIn ? 'Sign out' : 'Sign in'}
            </button>
          ) : <Link className="grid-button nav-signin" to="/chat"><LogIn size={16} />Sign in</Link>}
        </div>
      </header>
      <main id="content">{children}</main>
      <footer className="grid-footer">
        <div className="grid-container footer-content">
          <Link to="/" className="wordmark"><Hexagon size={18} /> Neuro<span>Grid</span></Link>
          <p>Open-source distributed inference. Simulated credits.</p>
          <a href="https://github.com/akmalm07/NeuroGrid">Source code <ArrowRight size={15} /></a>
        </div>
      </footer>
    </div>
  )
}

export function PageHeading({ title, children }: { title: string; children: ReactNode }) {
  return <header className="page-heading"><h1>{title}</h1><p>{children}</p></header>
}

export function networkReady(network: NetworkSnapshot | undefined): boolean {
  return Boolean(network?.verifier && SHARDS.every((_, index) =>
    network.nodes.some((node) => node.shard === index && ['online', 'working'].includes(node.status))))
}

export function Coverage({ network }: { network?: NetworkSnapshot }) {
  return (
    <div className="partition-grid">
      {SHARDS.map(({ title, layers, detail }, index) => {
        const count = network?.nodes.filter((node) => node.shard === index && ['online', 'working'].includes(node.status)).length
        const executing = network?.active?.stage === index && ['working', 'verifying'].includes(network.active.status)
        return (
          <article className={'partition-card' + (executing ? ' running' : '')} key={title}>
            <div className="partition-top"><Cpu aria-hidden="true" size={21} /><span>LAYERS {layers}</span></div>
            <h3>{title}</h3>
            <p>{detail}</p>
            {network && <div className="partition-status">
              <span className={'live-dot ' + (count ? 'available' : '')} />
              {executing ? 'Computing' : count === 1 ? '1 node online' : count + ' nodes online'}
            </div>}
          </article>
        )
      })}
    </div>
  )
}

export function StartLinks() {
  return (
    <div className="hero-actions">
      <Link className="grid-button hero-button filled" to="/downloads"><ArrowDownToLine size={19} />Start Computing</Link>
      <Link className="grid-button hero-button filled" to="/chat"><Cable size={19} />Try NeuroGrid</Link>
    </div>
  )
}
