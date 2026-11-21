import { Suspense, useState } from 'react'
import { Outlet } from 'react-router-dom'
import { AuthOverlay, DeepSpaceAuthProvider, RecordProvider, RecordScope, signOut, useAuth } from 'deepspace'
import { SCOPE_ID } from '../../constants'
import { schemas } from '../../schemas'
import { Shell } from '../../neurogrid/ui'

export default function AppLayout() {
  return <DeepSpaceAuthProvider><AppRoutes /></DeepSpaceAuthProvider>
}

function AppRoutes() {
  const { isLoaded, isSignedIn } = useAuth()
  const [showSignIn, setShowSignIn] = useState(false)
  return <Shell auth={{
    signedIn: Boolean(isSignedIn),
    action: () => { if (isSignedIn) void signOut(); else setShowSignIn(true) },
  }}>
    {isLoaded ? <RecordProvider allowAnonymous>
      <RecordScope roomId={SCOPE_ID} schemas={schemas}>
        <Suspense fallback={<p className="grid-container page-body">Loading…</p>}><Outlet context={setShowSignIn} /></Suspense>
      </RecordScope>
    </RecordProvider> : <p className="grid-container page-body" role="status">Loading NeuroGrid…</p>}
    {showSignIn && !isSignedIn && <AuthOverlay providers={['github', 'google']} onClose={() => setShowSignIn(false)} />}
  </Shell>
}

