import { useState } from 'react'
import { can, get, post } from '../lib/api'
import { usePoll, when } from '../lib/hooks'
import { Badge, Card, ErrorBox, Table } from '../lib/ui'

function Build({ id }) {
  const b = usePoll(() => get(`/builds/${id}`), 0, [id])
  const d = b.data
  if (!d) return null
  return (
    <Card title={`Build #${d.id} - ${d.project}`} actions={<Badge value={d.status} />}>
      <p className="muted">{d.message} - flash {d.flash_bytes} bytes ({d.flash_pct} %), RAM {d.ram_bytes} bytes - harness
        {' '}{d.harness_version} - sources {d.sources_sha256?.slice(0, 8)}</p>
      <Table rows={d.diagnostics} empty="No warnings or errors." cols={[{ label: 'severity', render: (x) => <Badge value={x.severity === 'error' ? 'FAILED' : x.severity} /> },
        { label: 'where', render: (x) => `${x.file || ''}:${x.line || ''}` }, { key: 'message', label: 'message' }]} />
    </Card>
  )
}

export default function Firmware({ role }) {
  const projects = usePoll(() => get('/projects'), 15000)
  const [project, setProject] = useState(null)
  const [file, setFile] = useState(null)
  const [build, setBuild] = useState(null)
  const [err, setErr] = useState(null)
  const builds = usePoll(() => get(`/builds?limit=20${project ? `&project=${project}` : ''}`), 10000, [project])
  const source = usePoll(() => (project && file ? get(`/projects/${project}/sources/${file}`) : Promise.resolve(null)), 0,
    [project, file])
  const p = (projects.data || []).find((x) => x.name === project)
  const startBuild = async () => {
    setErr(null)
    try {
      await post('/builds', { project })
      builds.reload()
    } catch (e) {
      setErr(e)
    }
  }
  return (
    <>
      <h2>Firmware</h2>
      <div className="grid">
        <Card title="Projects">
          <Table rows={projects.data} onRow={(r) => { setProject(r.name); setFile(null) }} cols={[
            { key: 'name', label: 'project' }, { label: 'files', render: (r) => r.files.length },
            { label: 'last build', render: (r) => (r.last_build ? <>#{r.last_build.id} <Badge value={r.last_build.status} /></> : '-') }]} />
        </Card>
        {p && (
          <Card title={`Project ${p.name}`} actions={can(role, 'agent') && <button className="primary" onClick={startBuild}>Build</button>}>
            <ErrorBox error={err} />
            <div className="row">{p.files.map((f) => <button key={f.name} className={file === f.name ? 'on' : ''}
              onClick={() => setFile(f.name)}>{f.name} ({f.size} B)</button>)}</div>
            {source.data && <pre>{source.data.content}</pre>}
          </Card>
        )}
      </div>
      <Card title={project ? `Builds of ${project}` : 'Builds'}>
        <Table rows={builds.data} onRow={(r) => setBuild(r.id)} cols={[{ key: 'id', label: '#' }, { key: 'project', label: 'project' },
          { label: 'status', render: (r) => <Badge value={r.status} /> }, { key: 'errors', label: 'errors' },
          { key: 'warnings', label: 'warnings' }, { label: 'when', render: (r) => when(r.started_at) }]} />
      </Card>
      {build && <Build id={build} />}
    </>
  )
}
