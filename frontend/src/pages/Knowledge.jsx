import { useState } from 'react'
import { can, get, post } from '../lib/api'
import { usePoll, when } from '../lib/hooks'
import { Badge, Card, ErrorBox, Field, Table } from '../lib/ui'

export default function Knowledge({ role }) {
  const stats = usePoll(() => get('/knowledge/stats'), 30000)
  const [q, setQ] = useState('')
  const [kind, setKind] = useState('')
  const [hits, setHits] = useState(null)
  const [open, setOpen] = useState(null)
  const [project, setProject] = useState('')
  const [err, setErr] = useState(null)
  const timeline = usePoll(() => (project ? get(`/projects/${project}/timeline?limit=100`) : Promise.resolve(null)), 0, [project])
  const [dec, setDec] = useState({ title: '', context: '', decision: '', consequences: '', project: '' })
  const search = async (e) => {
    e.preventDefault()
    setErr(null)
    try {
      setHits(await get(`/knowledge/search?q=${encodeURIComponent(q)}${kind ? `&kind=${kind}` : ''}&limit=30`))
    } catch (ex) {
      setErr(ex)
    }
  }
  const save = async () => {
    setErr(null)
    try {
      await post('/knowledge/decisions', { ...dec, project: dec.project || undefined })
      setDec({ title: '', context: '', decision: '', consequences: '', project: '' })
      stats.reload()
    } catch (ex) {
      setErr(ex)
    }
  }
  return (
    <>
      <h2>Knowledge</h2>
      <p className="muted">What the lab learned: bugs with their minimised inputs, fixes with root cause and diff,
        benchmarks, optimisations, failed builds and tests, decisions. Agents are given the relevant entries with
        every new task. {stats.data && `${stats.data.total} entries.`}</p>
      <Card title="Search">
        <form className="row" onSubmit={search}>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. division by zero hang" style={{ flex: 1 }} aria-label="search" />
          <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: 'auto' }} aria-label="kind">
            <option value="">any kind</option>{(stats.data?.kinds || []).map((k) => <option key={k}>{k}</option>)}
          </select>
          <button className="primary" disabled={q.trim().length < 2}>Search</button>
        </form>
        <ErrorBox error={err} />
        {hits && <Table rows={hits} onRow={(h) => setOpen(open?.id === h.id ? null : h)} empty="No match." cols={[
          { label: 'kind', render: (h) => <Badge value={h.kind} tone="info" /> }, { key: 'title', label: 'title' },
          { key: 'project', label: 'project' }, { label: 'build', render: (h) => h.build_id && `#${h.build_id}` },
          { label: 'match', render: (h) => <span className="muted">{h.snippet}</span> }, { key: 'score', label: 'score' }]} />}
        {open && <pre>{open.body}{`\n\nsource: ${open.source_type} #${open.source_id} - ${when(open.created_at)}`}</pre>}
      </Card>
      <div className="grid">
        <Card title="Project timeline" actions={<input placeholder="project" value={project} onChange={(e) => setProject(e.target.value)} />}>
          <Table rows={project ? timeline.data : []} empty="Type a project name." cols={[{ label: 'when', render: (e) => when(e.ts) },
            { key: 'type', label: 'what' }, { key: 'summary', label: 'summary' }]} />
        </Card>
        {can(role, 'agent') && (
          <Card title="Write down a decision">
            {['title', 'context', 'decision', 'consequences', 'project'].map((k) => (
              <Field key={k} label={k}>{['context', 'decision', 'consequences'].includes(k)
                ? <textarea value={dec[k]} onChange={(e) => setDec({ ...dec, [k]: e.target.value })} />
                : <input value={dec[k]} onChange={(e) => setDec({ ...dec, [k]: e.target.value })} />}</Field>
            ))}
            <button className="primary" disabled={dec.title.length < 3 || dec.context.length < 3 || dec.decision.length < 3} onClick={save}>Save decision</button>
          </Card>
        )}
      </div>
    </>
  )
}
