/** Persisted OrbitControls viewpoint per simulator (localStorage). */

export interface SavedCamera {
  position: [number, number, number]
  target: [number, number, number]
}

const PREFIX = 'heywalk.camera.'

function isVec(value: unknown): value is [number, number, number] {
  return (
    Array.isArray(value) &&
    value.length === 3 &&
    value.every((n) => typeof n === 'number' && Number.isFinite(n))
  )
}

export function loadSavedCamera(key: string): SavedCamera | null {
  try {
    const raw = localStorage.getItem(PREFIX + key)
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return null
    const { position, target } = parsed as Record<string, unknown>
    if (!isVec(position) || !isVec(target)) return null
    return { position, target }
  } catch {
    return null
  }
}

export function saveCamera(
  key: string,
  position: { x: number; y: number; z: number },
  target: { x: number; y: number; z: number },
): void {
  try {
    const saved: SavedCamera = {
      position: [position.x, position.y, position.z],
      target: [target.x, target.y, target.z],
    }
    localStorage.setItem(PREFIX + key, JSON.stringify(saved))
  } catch {
    // Private mode / quota: the default view still works.
  }
}

export function clearSavedCamera(key: string): void {
  try {
    localStorage.removeItem(PREFIX + key)
  } catch {
    // Nothing to clear.
  }
}
