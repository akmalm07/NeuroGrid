import { Activity, BadgeCheck, Cpu } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useGridRevision, useNetwork } from '../../neurogrid/live'
import { SHARDS } from '../../neurogrid/protocol'
import { Coverage, networkReady, PageHeading } from '../../neurogrid/ui'

export default function Network() {
  const { result, failed } = useNetwork(useGridRevision())
  const online = result?.nodes.filter((node) => node.status === 'online' || node.status === 'working').length
  const ready = networkReady(result)
  return <div className="grid-container page-body">
    <PageHeading title="Network">See which computers are available, what they host, and where inference is running.</PageHeading>
    {failed && <p className="form-error" role="status">Could not refresh the network. Displayed data may be stale.</p>}
    <div className="network-metrics">
      <article className="surface side-panel"><Cpu size={22} /><h2>Workers online</h2><strong className="large-value">{online ?? '—'}</strong></article>
      <article className="surface side-panel"><BadgeCheck size={22} /><h2>Reference verifier</h2><strong>{result ? result.verifier ? 'Online' : 'Offline' : 'Checking'}</strong></article>
      <article className="surface side-panel"><Activity size={22} /><h2>Inference</h2><strong>{result?.active ? 'In progress' : ready ? 'Ready' : result ? 'Waiting for capacity' : 'Checking'}</strong>
        {result?.active && <p>Partition {result.active.stage + 1} · {result.active.count} tokens verified</p>}</article>
    </div>
    <section className="network-section">
      <div className="section-heading"><div><h2>Partition coverage</h2><p>Inference requires an online worker for each layer range.</p></div>
        <Link className="grid-button outlined" to="/downloads">Add a worker</Link></div>
      <Coverage network={result} />
    </section>
    <section className="surface registry network-section">
      <h2>Registered nodes</h2>
      {result?.nodes.length ? <div className="table-scroll"><table><thead><tr><th>Node ID</th><th>Partition</th><th>Layers</th><th>Status</th></tr></thead>
        <tbody>{result.nodes.map((node) => <tr key={node.id}>
          <td><code>{node.id.split('-')[0]}</code></td><td>{SHARDS[node.shard].title}</td><td>{SHARDS[node.shard].layers}</td>
          <td><span className={'live-dot ' + (node.status === 'online' || node.status === 'working' ? 'available' : '')} />{node.status}</td>
        </tr>)}</tbody></table></div> : <p>{result ? 'No nodes have paired with this network yet.' : 'Loading registered nodes…'}</p>}
    </section>
  </div>
}
