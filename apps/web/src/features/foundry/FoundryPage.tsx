import { useFoundryStore } from '../../stores/foundry'
import { FoundryDashboard } from './FoundryDashboard'
import { FoundryWorkspace } from './FoundryWorkspace'

/**
 * Robot Foundry shell. The IDE header stays mounted; this page owns the
 * dashboard/workspace split and keeps the rest of the IDE state untouched.
 */
export function FoundryPage() {
  const activeId = useFoundryStore((s) => s.activeId)
  return (
    <main className="flex min-h-0 flex-1 flex-col overflow-hidden bg-base">
      {activeId ? <FoundryWorkspace /> : <FoundryDashboard />}
    </main>
  )
}
