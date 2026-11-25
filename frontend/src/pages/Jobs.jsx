import { useState } from 'react'
import { can, get, post } from '../lib/api'
import { usePoll, when } from '../lib/hooks'
import { Badge, Card, ErrorBox, Table } from '../lib/ui'

export function JobLogs({ jobId }) {
  const { data } = usePoll(() => get(`/jobs/${jobId}/logs?after=0`), 2000, [jobId])
  return (
    <pre className="log">
      {(data?.lines || []).map((l) => `${l.ts.slice(11, 19)}  ${l.text}`).join('\n') || 'no log lines yet'}
    </pre>
  )
}

export default function Jobs({ role }) {
  const [status, setStatus] = useState('')
  const [open, setOpen] = useState(null)
  const [err, setErr] = useState(null)
  const { data, reload } = usePoll(() => get(`/jobs?limit=50${status ? `&status=${status}` : ''}`), 3000, [status])
  const cancel = async (id) => {
    try {
      await post(`/jobs/${id}/cancel`)
      reload()
    } catch (e) {
      setErr(e)
    }
  }
  return (
    <>
      <h2>Jobs</h2>
      <Card title="Background jobs" actions={
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="filter by status">
          <option value="">all</option>
          {['AWAITING_APPROVAL', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'REJECTED', 'EXPIRED', 'CANCELLED']
            .map((s) => <option key={s}>{s}</option>)}
        </select>}>
        <ErrorBox error={err} />
        <Table rows={data} onRow={(r) => setOpen(open === r.id ? null : r.id)} cols={[
          { key: 'id', label: '#' }, { key: 'kind', label: 'kind' },
          { label: 'status', render: (r) => <Badge value={r.status} /> },
          { key: 'summary', label: 'what' }, { key: 'requested_by', label: 'by' },
          { label: 'created', render: (r) => when(r.created_at) },
          { label: '', render: (r) => can(role, 'agent') && ['QUEUED', 'AWAITING_APPROVAL'].includes(r.status)
            ? <button onClick={(e) => { e.stopPropagation(); cancel(r.id) }}>cancel</button> : null },
        ]} />
      </Card>
      {open && (
        <Card title={`Job #${open} log`}>
          <JobLogs jobId={open} />
        </Card>
      )}
    </>
  )
}
