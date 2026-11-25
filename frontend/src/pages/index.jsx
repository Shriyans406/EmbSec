// Every page of the dashboard. `need` is the least role that sees it in the menu (the API checks again anyway).
import Device from './Device'
import Jobs from './Jobs'
import Overview from './Overview'

export const PAGES = [
  { path: 'overview', label: 'Overview', component: Overview },
  { path: 'device', label: 'Device', component: Device },
  { path: 'jobs', label: 'Jobs', component: Jobs },
]
