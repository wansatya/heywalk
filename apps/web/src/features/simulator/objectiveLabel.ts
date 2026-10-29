import type { RunObjective } from '../../api/types'

/** "env_box_1" -> "box 1": the UI names props, never raw body ids. */
export function propLabel(body: string): string {
  return body.replace(/^env_/, '').replace(/_/g, ' ')
}

/** Short label for one phase (no prefix): "kick the ball", "flag", … */
function phaseLabel(objective: RunObjective): string {
  if (objective.type === 'kick') return 'kick the ball'
  if (objective.type === 'wander') return 'wander'
  if (objective.target_body) return `go to the ${propLabel(objective.target_body)}`
  if (objective.type === 'touch' && objective.target)
    return `touch (${objective.target[0].toFixed(1)}, ${objective.target[1].toFixed(1)})`
  if (objective.target) return `(${objective.target[0].toFixed(1)}, ${objective.target[1].toFixed(1)})`
  return objective.type === 'go_to' ? 'flag' : objective.type
}

/** One-line description of what a run grades, for the run controls chip. */
export function objectiveLabel(objective: RunObjective | null): string {
  if (!objective) return 'Objective: flag'
  return `Objective: ${phaseLabel(objective)}`
}

/** Phase sequence for the chip: "Objective: kick the ball → flag". */
export function objectivesLabel(objectives: RunObjective[]): string {
  if (objectives.length === 0) return 'Objective: flag'
  return `Objective: ${objectives.map(phaseLabel).join(' → ')}`
}
