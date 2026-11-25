// Every page of the dashboard. `need` is the least role that sees it in the menu (the API checks again anyway).
import Jobs from './Jobs'

const Soon = ({ name }) => () => <p className="muted">The {name} page arrives in a later step.</p>

export const PAGES = [
  { path: 'overview', label: 'Overview', component: Soon({ name: 'overview' }) },
  { path: 'jobs', label: 'Jobs', component: Jobs },
]
