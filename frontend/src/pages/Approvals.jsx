import { useCallback, useState } from 'react'
import { can, get, post } from '../lib/api'
import { usePoll, when } from '../lib/hooks'
import { useLive } from '../lib/live'
import { Badge, Card, ConfirmDialog, Empty, ErrorBox, Table } from '../lib/ui'

function Proposal({ job, role, onDone }) {
  const [open, setOpen] = useState(false)
  const [err, setErr] = useState(null)
  const checks = job.params?.checks || []
  const operator = can(role, 'operator')
  const reject = async () => {
    try {
      await post(`/jobs/${job.id}/reject`, { reason: 'rejected in the dashboard' })
      onDone()
    } catch (e) {
      setErr(e)
    }
  }
  return (
    <Card title={`Job #${job.id}: ${job.summary}`} actions={<Badge value={job.status} />}>
      <p className="muted">proposed by {job.requested_by} - {when(job.created_at)} - expires {when(job.expires_at)}</p>
      <Table rows={checks} cols={[{ label: 'check', key: 'name' }, { label: 'result', render: (c) => <Badge value={c.status} /> },
        { label: 'detail', key: 'detail' }]} empty="No pre-flight checks recorded." />
      <ErrorBox error={err} />
      {operator ? (
        <div className="row end" style={{ marginTop: 8 }}>
          <button onClick={reject}>Reject</button>
          <button className="danger" disabled={checks.some((c) => c.status === 'FAIL')} onClick={() => setOpen(true)}>
            Approve ...</button>
        </div>
      ) : <p className="muted">Only an operator can approve or reject. Agents never can.</p>}
      {open && (
        <ConfirmDialog title={`Approve job #${job.id}?`} token={job.confirm_token || ''} onClose={() => setOpen(false)}
          onConfirm={async (typed) => { await post(`/jobs/${job.id}/approve`, { confirm: typed }); onDone() }}>
          <p>This will overwrite the firmware on the board ({job.summary}). If the new firmware does not come up
            healthy, the deploy engine restores the previous one by itself.</p>
        </ConfirmDialog>
      )}
    </Card>
  )
}

export default function Approvals({ role }) {
  const waiting = usePoll(() => get('/jobs?status=AWAITING_APPROVAL&limit=50'), 10000)
  const deployments = usePoll(() => get('/deployments?limit=20'), 15000)
  const kg = usePoll(() => get('/known-good'), 30000)
  const { reload: reloadWaiting } = waiting
  const { reload: reloadDeployments } = deployments
  const reload = useCallback(() => {
    reloadWaiting()
    reloadDeployments()
  }, [reloadWaiting, reloadDeployments])
  useLive(useCallback((m) => { if (m.type === 'job') reload() }, [reload]))
  return (
    <>
      <h2>Approval centre</h2>
      <p className="muted">Nothing is flashed onto the board until a human approves it here (or in a terminal) by
        typing the confirmation text. Pipelines, remediations and optimisations pause until then; resume them on
        their page after deciding.</p>
      {waiting.data?.length === 0 && <Empty>No proposal is waiting.</Empty>}
      {(waiting.data || []).map((j) => <Proposal key={j.id} job={j} role={role} onDone={reload} />)}
      <div className="grid">
        <Card title="Deployments">
          <Table rows={deployments.data} cols={[{ key: 'id', label: '#' }, { key: 'kind', label: 'kind' },
            { label: 'status', render: (d) => <Badge value={d.status} /> }, { label: 'build', render: (d) => d.build_id && `#${d.build_id}` },
            { key: 'approved_by', label: 'approved by' }, { label: 'when', render: (d) => when(d.started_at) }]} />
        </Card>
        <Card title="Fallback firmware">
          {kg.data && (
            <Table rows={[{ id: 1, name: 'known-good', ...kg.data.known_good }, { id: 2, name: 'factory backup', ...kg.data.factory_backup }]}
              cols={[{ key: 'name', label: 'image' }, { label: 'present', render: (r) => <Badge value={String(!!r.present)} /> },
                { label: 'valid', render: (r) => <Badge value={String(!!r.valid)} /> }, { key: 'label', label: 'what' }]} />
          )}
        </Card>
      </div>
    </>
  )
}
