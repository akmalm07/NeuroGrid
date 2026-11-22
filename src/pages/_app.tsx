import { Suspense } from 'react'
import { Outlet, useRouteError } from 'react-router-dom'
import { ErrorScreen } from '../components/ErrorScreen'

export default function App() {
  return (
    <div data-testid="app-root" className="min-h-screen bg-background text-foreground">
      <Suspense fallback={<p role="status">Loading NeuroGrid…</p>}>
        <Outlet />
      </Suspense>
    </div>
  )
}

export function Catch() {
  return <ErrorScreen error={useRouteError()} />
}
