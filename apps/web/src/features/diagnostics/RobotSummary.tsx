import { Bot } from 'lucide-react'
import type { RobotPackage } from '../../api/types'

/**
 * Validated robot package returned by generation/correction: the MJCF the
 * physics backend will load, its actuated joints, and the automated tests the
 * platform derived from those joints (AGENTS.md §7).
 */
export function RobotSummary({ robot }: { robot: RobotPackage }) {
  return (
    <div className="space-y-2.5 rounded-xl border border-border-subtle bg-stone-50 p-3 shadow-xs">
      <p className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-stone-500">
        <Bot size={13} className="text-sky-600" />
        Robot package
      </p>
      <p className="rounded-md border border-border-subtle/50 bg-white px-2.5 py-1 font-mono text-xs text-stone-600">
        {robot.model_path}
      </p>

      {robot.joints.length > 0 ? (
        <div className="space-y-1.5">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-stone-500">Actuated joints</p>
          <div className="overflow-x-auto rounded-lg border border-border-subtle/50 bg-white p-2">
            <table className="w-full font-mono text-xs">
              <thead>
                <tr className="border-b border-border-subtle/50 text-left text-[11px] text-stone-400">
                  <th className="pb-1.5 pr-2 font-semibold">joint</th>
                  <th className="pb-1.5 pr-2 font-semibold">type</th>
                  <th className="pb-1.5 pr-2 font-semibold">actuator</th>
                  <th className="pb-1.5 font-semibold">range</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border-subtle/30 text-stone-600">
                {robot.joints.map((joint) => (
                  <tr key={joint.name}>
                    <td className="py-1.5 pr-2 text-stone-700">{joint.name}</td>
                    <td className="py-1.5 pr-2 text-stone-400">{joint.type}</td>
                    <td className="py-1.5 pr-2 text-stone-400">{joint.actuator || '—'}</td>
                    <td className="py-1.5 text-stone-400 tabular-nums">
                      {joint.lower === null || joint.upper === null
                        ? 'unbounded'
                        : `${joint.lower.toFixed(2)} … ${joint.upper.toFixed(2)}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <p className="text-xs text-stone-400">No actuated joints found in the model.</p>
      )}

      {robot.tests.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-stone-500">
            Planned automated tests ({robot.tests.length})
          </p>
          <div className="space-y-1 rounded-lg border border-border-subtle/50 bg-white p-2.5">
            {robot.tests.map((test) => (
              <p key={test.id} className="font-mono text-xs text-stone-600 tabular-nums">
                {test.joint} → {test.target.toFixed(3)} ± {test.tolerance.toFixed(3)}
              </p>
            ))}
          </div>
        </div>
      )}

      {robot.notes.length > 0 && (
        <div className="space-y-1 rounded-lg border border-amber-200 bg-amber-50 p-2.5">
          {robot.notes.map((note, i) => (
            <p key={i} className="text-xs text-amber-700 leading-relaxed">
              • {note}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}
