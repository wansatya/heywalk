import type { DiffDriveWorldConfig } from '../../api/types'

// Fallback shown before a run is selected. Mirrors the backend
// DEFAULT_WORLD (services/api/app/simulator/__init__.py) so the empty
// viewport already shows the arena the first run will use.
export const DEFAULT_WORLD: DiffDriveWorldConfig = {
  width: 10,
  height: 8,
  start: { x: 1.0, y: 1.0, theta: 0 },
  goal: { x: 8.5, y: 6.5, radius: 0.4 },
  obstacles: [
    { x: 4.0, y: 1.0, w: 0.6, h: 4.0 },
    { x: 6.0, y: 4.0, w: 0.6, h: 3.0 },
  ],
  time_limit: 60.0,
}
