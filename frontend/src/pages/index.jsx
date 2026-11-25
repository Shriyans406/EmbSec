// Every page of the dashboard. `need` is the least role that sees it in the menu (the API checks again anyway).
import Agents from './Agents'
import Approvals from './Approvals'
import Audit from './Audit'
import Device from './Device'
import Firmware from './Firmware'
import Fuzz from './Fuzz'
import Jobs from './Jobs'
import Knowledge from './Knowledge'
import Overview from './Overview'
import Profiling from './Profiling'
import Tests from './Tests'
import { Optimisations, Pipelines, Remediations } from './workflows'

export const PAGES = [
  { path: 'overview', label: 'Overview', component: Overview },
  { path: 'device', label: 'Device', component: Device },
  { path: 'agents', label: 'Agents', component: Agents },
  { path: 'firmware', label: 'Firmware', component: Firmware },
  { path: 'pipelines', label: 'Pipelines', component: Pipelines },
  { path: 'approvals', label: 'Approval centre', component: Approvals },
  { path: 'tests', label: 'Tests', component: Tests },
  { path: 'fuzz', label: 'Fuzzing', component: Fuzz },
  { path: 'remediations', label: 'Remediation', component: Remediations },
  { path: 'profiling', label: 'Profiling', component: Profiling },
  { path: 'optimisations', label: 'Optimisation', component: Optimisations },
  { path: 'knowledge', label: 'Knowledge', component: Knowledge },
  { path: 'jobs', label: 'Jobs', component: Jobs },
  { path: 'audit', label: 'Audit log', component: Audit, need: 'operator' },
]
