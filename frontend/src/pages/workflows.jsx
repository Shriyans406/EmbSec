// The three workflow kinds, described for the shared workflow page.
import { Field } from '../lib/ui'
import { workflowPage } from './Workflow'

const input = (form, setForm, key, props = {}) => (
  <input value={form[key] ?? ''} onChange={(e) => setForm({ ...form, [key]: e.target.value })} {...props} />
)

export const Pipelines = workflowPage({
  path: 'pipelines', api: 'pipelines', job: 'pipeline', title: 'Pipelines', one: 'Pipeline', many: 'pipelines',
  intro: 'A requirement becomes a specification, code, a build, a security scan, a deployment proposal (a human '
    + 'approves) and functional tests on the board - with a report at the end.',
  initial: { requirement: '', deploy: true }, diff: false,
  body: (f) => ({ requirement: f.requirement, deploy: f.deploy }),
  describe: (x) => `${x.project ? `${x.project}: ` : ''}${x.requirement}`,
  fields: (f, set) => <>
    <Field label="requirement"><textarea value={f.requirement} onChange={(e) => set({ ...f, requirement: e.target.value })}
      placeholder="APP ADD a b answers with the sum; anything else is an argument error." /></Field>
    <label className="row"><input type="checkbox" style={{ width: 'auto' }} checked={f.deploy}
      onChange={(e) => set({ ...f, deploy: e.target.checked })} /> propose a deployment and test on the board</label>
  </>,
})

export const Remediations = workflowPage({
  path: 'remediations', api: 'remediations', job: 'remediation', title: 'Remediation', one: 'Remediation',
  many: 'remediations', diff: true,
  intro: 'A reproducible bug (a fuzz finding) is reproduced, its root cause found and patched, the fix built and '
    + 'deployed after approval, the bug retested and everything else checked for regressions - or rolled back.',
  initial: { finding_id: '' },
  body: (f) => ({ finding_id: Number(f.finding_id) }),
  describe: (x) => `finding #${x.finding_id}${x.project ? ` in ${x.project}` : ''}${x.root_cause ? ` - ${x.root_cause}` : ''}`,
  fields: (f, set) => <Field label="fuzz finding number (see the fuzzing page)">{input(f, set, 'finding_id', { type: 'number', min: 1 })}</Field>,
})

export const Optimisations = workflowPage({
  path: 'optimisations', api: 'optimizations', job: 'optimization', title: 'Optimisation', one: 'Optimisation',
  many: 'optimisations', diff: true,
  intro: 'The Performance agent proposes one change, the Firmware agent writes it, and the two versions are '
    + 'measured on the board. The change is kept only if it is better than the noise allows and still correct.',
  initial: { project: '', goal: 'speed', min_gain_pct: 10 },
  body: (f) => ({ project: f.project, goal: f.goal, min_gain_pct: Number(f.min_gain_pct) }),
  describe: (x) => `${x.project}: ${x.goal}${x.target ? ` of ${x.target}` : ''} (base #${x.base_build ?? '-'} -> #${x.variant_build ?? '-'})`,
  fields: (f, set) => <>
    <Field label="project (must be on the board)">{input(f, set, 'project')}</Field>
    <div className="row">
      <select value={f.goal} onChange={(e) => set({ ...f, goal: e.target.value })} style={{ width: 'auto' }} aria-label="goal">
        <option value="speed">faster</option><option value="flash">less flash</option><option value="ram">less RAM</option>
      </select>
      <span className="muted">at least</span>{input(f, set, 'min_gain_pct', { type: 'number', min: 1, max: 90, style: { width: 90 } })}<span>%</span>
    </div>
  </>,
})
