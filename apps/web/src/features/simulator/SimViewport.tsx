import { Suspense, lazy } from 'react'
import { useExperimentStore } from '../../stores/experiment'
import { isMujocoBackend, isMujocoWorld } from '../../api/types'
import { JointViewport } from './JointViewport'
import { SimViewportFallback } from './SimViewportFallback'

// The 3D arena pulls in three.js; keep it out of the initial bundle.
const WorldViewport = lazy(() =>
  import('./WorldViewport').then((m) => ({ default: m.WorldViewport })),
)

/**
 * Picks the visualization that matches the selected run's backend.
 *
 * With no run selected the MuJoCo view is still shown: a fresh sequence is
 * created with MicroDuck installed, so the duck and its composed scene are
 * visible in the STAND preview before anything runs. Showing the preview only
 * loads meshes — it never creates a run or starts the runner, and Start stays
 * disabled until the sequence has a task and a controller.
 */
export function SimViewport() {
  const runDetail = useExperimentStore((s) => s.runDetail)
  const backend = runDetail?.backend ?? runDetail?.world_config?.backend

  if (!runDetail) return <JointViewport />

  if (isMujocoBackend(backend) || isMujocoWorld(runDetail.world_config)) {
    // Deliberately NOT keyed per run: remounting would destroy the 3D
    // camera, so orbiting/zooming would reset on every Start. The view
    // follows the run automatically unless the user picks a tab, and an
    // explicit tab choice persisting across runs is the calmer behavior.
    return <JointViewport />
  }

  return (
    <Suspense fallback={<SimViewportFallback />}>
      <WorldViewport />
    </Suspense>
  )
}
