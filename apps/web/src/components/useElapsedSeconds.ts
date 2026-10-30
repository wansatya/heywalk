import { useEffect, useState } from 'react'

/** Seconds since `active` became true; resets to 0 when it turns false. */
export function useElapsedSeconds(active: boolean): number {
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    if (!active) {
      setElapsed(0)
      return
    }
    const started = Date.now()
    const timer = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - started) / 1000)),
      500,
    )
    return () => window.clearInterval(timer)
  }, [active])
  return elapsed
}
