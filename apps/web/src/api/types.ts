// Shared API shapes. Mirrors AGENTS.md sections 10-11.

export type RunStatus =
  | 'queued'
  | 'preparing'
  | 'running'
  | 'evaluating'
  | 'succeeded'
  | 'failed'
  | 'timeout'
  | 'cancelled'
  | 'error'

export const TERMINAL_RUN_STATUSES: RunStatus[] = [
  'succeeded',
  'failed',
  'timeout',
  'cancelled',
  'error',
]

export function isTerminalStatus(status: RunStatus): boolean {
  return TERMINAL_RUN_STATUSES.includes(status)
}

/**
 * Simulation backend for a run (AGENTS.md §7).
 *
 * `mujoco` is the default: a real physics model is loaded, driven through an
 * emulated ros2_control position interface, and validated by an automated
 * joint-test suite. `differential_drive` is the legacy deterministic 2D arena.
 */
export type Backend = 'mujoco' | 'differential_drive'

export const BACKEND_LABELS: Record<Backend, string> = {
  mujoco: 'MuJoCo',
  differential_drive: '2D diff-drive',
}

export function isMujocoBackend(backend: string | null | undefined): boolean {
  return backend === 'mujoco'
}

export interface Project {
  id: string
  name: string
  task_description: string
  created_at: string
  updated_at: string
  /** Grading phases for the current controller (empty = flag default). */
  objectives?: RunObjective[] | null
  /** @deprecated Legacy singular; read objectives instead. */
  objective?: RunObjective | null
}

export interface ProjectFileEntry {
  path: string
  version: number
  updated_at?: string
}

export interface ProjectFileContent extends ProjectFileEntry {
  content: string
}

export interface GeneratedFile {
  path: string
  content: string
}

export interface TraceStep {
  stage: string
  status: string
  ms: number
  detail: string
}

// ---- Robot description (validated robot package) ----

export interface RobotJoint {
  name: string
  type: string
  actuator: string
  lower: number | null
  upper: number | null
}

export interface PlannedTest {
  id: string
  joint: string
  target: number
  tolerance: number
  kind?: string
  hold_s?: number
  timeout_s?: number
}

/** Validated robot package returned with a generation or correction. */
export interface RobotPackage {
  model_path: string
  joints: RobotJoint[]
  tests: PlannedTest[]
  notes: string[]
}

/** A bundled, platform-owned robot the user can install without the model. */
export interface StarterRobot {
  id: string
  name: string
  summary: string
  attribution: string
  license: string
  source_url: string
  model_path: string
  joints: RobotJoint[]
  tests: PlannedTest[]
  /** `bundled` platform robots vs `foundry` generated robots. */
  source?: 'bundled' | 'foundry' | string
  foundry_id?: string | null
  thumbnail_asset_url?: string | null
}

export interface RunObjective {
  type: 'go_to' | 'touch' | 'wander' | 'kick'
  target: [number, number] | null
  /** Composed prop body to reach (e.g. "env_box_1"), resolved live by the runner. */
  target_body?: string | null
}

export interface GenerationResult {
  summary: string
  assumptions: string[]
  files: GeneratedFile[]
  expected_behavior: string
  safety_considerations: string
  objectives?: RunObjective[] | null
  /** @deprecated Legacy singular; read objectives instead. */
  objective?: RunObjective | null
  robot?: RobotPackage | null
  trace?: TraceStep[]
  model_name?: string
  latency_ms?: number | null
}

export interface FixResult {
  failure_hypothesis: string
  evidence: string[]
  proposed_change: string
  updated_files: GeneratedFile[]
  expected_improvement: string
  robot?: RobotPackage | null
  trace?: TraceStep[]
  model_name?: string
  latency_ms?: number | null
}

export interface ModelRequest {
  id: number
  project_id: string
  run_id: string | null
  purpose: string
  model_name: string
  prompt_version: string
  status: string
  latency_ms: number | null
  error_summary: string | null
  created_at: string | null
}

export interface RunMetrics {
  success: boolean
  collision_count: number
  goal_distance: number
  completion_time: number
  path_length: number
  emergency_stop_latency: number | null
  evaluator_version: string
  /** MuJoCo automated-test metrics; 0 for 2D runs. */
  tests_total: number
  tests_passed: number
  max_joint_error: number
  total_contacts: number
}

export interface EvaluationReport {
  success: boolean
  reasons: string[]
  evaluator_version: string
}

/** Objective evidence for one automated joint test (AGENTS.md §8). */
export interface TestResult {
  id: string
  kind: string
  /** Joint-test fields (absent on locomotion results). */
  joint?: string
  target?: number
  tolerance: number
  final_position?: number
  final_error: number
  max_error?: number
  max_velocity?: number
  max_effort?: number
  settling_time?: number | null
  duration?: number
  contacts: number
  max_contact_force?: number
  passed: boolean
  reason: string
  estop_latency: number | null
  /** Locomotion evidence (only when kind === 'locomotion'). */
  distance_m?: number
  min_flag_distance_m?: number
  final_flag_distance_m?: number
  min_trunk_z?: number
  fallen?: boolean
  outcome?: string
  /** Which objective this walk graded (go_to | touch | wander | kick). */
  objective?: string
  target_body?: string | null
  ball_touched?: boolean
  ball_displacement_m?: number
  /** Ordered phase this result belongs to (1-based) and the plan size. */
  phase?: number
  phases?: number
  /** Sim time at the verdict (whole run) and this phase's own window. */
  duration_s?: number
  phase_duration_s?: number
  phase_budget_s?: number
}

/** One actuated joint as reported by the MuJoCo state interface. */
export interface JointState {
  name: string
  position: number
  velocity: number
  effort: number
  target: number
}

/** One rigid body pose from MuJoCo (pos xyz, quat wxyz). */
export interface BodyPose {
  body: string
  pos: [number, number, number]
  quat: [number, number, number, number]
}

/** One visual mesh placement on a MicroDuck body (from the official MJCF). */
export interface RobotVisualGeom {
  body: string
  mesh: string
  file: string
  pos: [number, number, number]
  quat: [number, number, number, number]
  rgba: [number, number, number, number]
}

/** One primitive geom of a composed-environment body (MuJoCo units). */
export interface RobotEnvGeom {
  name: string
  type: 'box' | 'sphere' | 'cylinder'
  size: number[]
  pos: [number, number, number]
  quat: [number, number, number, number]
  rgba: [number, number, number, number]
  role: string
}

/** One static environment body: obstacle crate, ball, or goal flag. */
export interface RobotEnvBody {
  body: string
  pos: [number, number, number]
  geoms: RobotEnvGeom[]
}

/** Bundled-robot model metadata served for the UI + 3D renderer. */
export interface RobotModel {
  robot_id: string
  joints: string[]
  actuators: string[]
  sensors: string[]
  bodies: string[]
  joint_ranges: Record<string, { lower: number | null; upper: number | null }>
  keyframes: string[]
  mesh_files: string[]
  timestep: number
  control_hz: number
  source_url: string
  source_commit: string
  license: string
  hierarchy: Array<{ body: string | null; parent: string | null }>
  /** Mesh placements: 70 entries attaching 38 STLs to 15 bodies. */
  visual_geoms: RobotVisualGeom[]
  /** STAND keyframe world poses for the no-telemetry preview. */
  default_poses: BodyPose[]
  /** Obstacle crates, ball, and goal flag of the composed scene. */
  environment: RobotEnvBody[]
  assets_ok: boolean
  missing_assets: string[]
  error?: string
}

export interface Run {
  id: string
  project_id: string
  parent_run_id: string | null
  controller_version: number
  status: RunStatus
  backend: Backend
  world_config: WorldConfig | null
  seed: number
  started_at: string | null
  finished_at: string | null
  exit_code: number | null
  failure_summary: string | null
  metrics: RunMetrics | null
  evaluation: EvaluationReport | null
  /** Automated joint-test evidence (empty for 2D runs). */
  tests: TestResult[]
  /** True while the run's physics is suspended by pause. */
  paused?: boolean
}

export interface Pose {
  x: number
  y: number
  theta: number
}

export interface ObstacleRect {
  x: number
  y: number
  w: number
  h: number
}

/** Legacy deterministic 2D arena (AGENTS.md §7). */
export interface DiffDriveWorldConfig {
  backend?: 'differential_drive'
  width: number
  height: number
  start: Pose
  goal: { x: number; y: number; radius: number }
  obstacles: ObstacleRect[]
  /** Sim-time budget in seconds, or null for unlimited (goal/collision/e-stop/cancel end the run). */
  time_limit?: number | null
}

/** MuJoCo scenario: physics model + automated joint tests. */
export interface MujocoWorldConfig {
  backend: 'mujoco'
  dt: number
  control_hz: number
  telemetry_hz: number
  /** Sim-time budget per test case, or null for unlimited. */
  time_limit: number | null
  seed: number
  tests?: PlannedTest[]
  estop_after_s: number | null
  estop_latency_limit: number
  collision_force_threshold: number
  ignore_root_contacts: boolean
  joint_velocity_tolerance: number
  model_path: string
  urdf_path: string
  ros2_control_path: string
  /** Arrange-mode robot spawn (floor x/y + facing yaw), or null for STAND. */
  spawn_xy?: [number, number] | null
  spawn_yaw?: number | null
  /** Arrange-mode prop floor spots, keyed by env body name. */
  env_layout?: Record<string, [number, number]> | Array<{ body: string; x: number; y: number }> | null
  /** Trunk welded to a test stand (default for MicroDuck) vs free-standing. */
  fix_base?: boolean
  /** Locomotion run: walk with the built-in policy, steered by the controller. */
  locomotion?: boolean
  /** Task phases this run grades (validated by the runner). */
  objectives?: RunObjective[] | null
  /** First phase, for legacy readers. */
  objective?: RunObjective | null
}

/** Robot spawn pose on the floor plane (MuJoCo coords, radians). */
export interface RobotSpawn {
  x: number
  y: number
  yaw: number
}

/** Arrange-mode scene layout for the next run (null robot = STAND spawn). */
export interface SimLayout {
  robot: RobotSpawn | null
  props: Record<string, { x: number; y: number }>
}

export const DEFAULT_SIM_LAYOUT: SimLayout = { robot: null, props: {} }

export function isDefaultLayout(layout: SimLayout): boolean {
  return layout.robot === null && Object.keys(layout.props).length === 0
}

export type WorldConfig = DiffDriveWorldConfig | MujocoWorldConfig

export function isMujocoWorld(
  world: WorldConfig | null | undefined,
): world is MujocoWorldConfig {
  return !!world && (world as MujocoWorldConfig).backend === 'mujoco'
}

export function isDiffDriveWorld(
  world: WorldConfig | null | undefined,
): world is DiffDriveWorldConfig {
  return !!world && !isMujocoWorld(world) && 'start' in world
}

export interface TelemetrySample {
  t: number
  x: number
  y: number
  theta: number
  v: number
  omega: number
  collision?: boolean
  goal_reached?: boolean
  estop_active?: boolean
  min_range?: number
  /** MuJoCo joint telemetry (absent for 2D runs). */
  joints?: JointState[] | null
  contacts?: number | null
  max_contact_force?: number | null
  /** Set while a specific automated test case is executing. */
  test_id?: string | null
  /** Official-model telemetry (absent unless the scenario enables it). */
  fallen?: boolean | null
  trunk_z?: number | null
  expected_contacts?: number | null
  /** Twist commanded to the walking policy (locomotion runs only). */
  twist?: [number, number, number] | null
  sensors?: Record<string, number[]> | null
  bodies?: BodyPose[] | null
}

export type StreamEvent =
  | { type: 'status'; status: RunStatus }
  | { type: 'log'; line: string }
  | { type: 'telemetry'; sample: TelemetrySample }
  | { type: 'evaluation'; evaluation: EvaluationReport; metrics: RunMetrics | null }
  | { type: 'error'; message: string }

export interface ComparisonRunSummary {
  run_id: string
  status: RunStatus
  controller_version: number
  failure_summary: string | null
  metrics: RunMetrics | null
  backend: Backend
  tests_total: number
  tests_passed: number
}

export interface RunComparison {
  baseline: ComparisonRunSummary | null
  current: ComparisonRunSummary
}

// ---- Robot Foundry --------------------------------------------------------

export type FoundryStatus =
  | 'DRAFT'
  | 'ANALYZING'
  | 'RECONSTRUCTING'
  | 'ASSEMBLING'
  | 'GENERATING'
  | 'VALIDATING'
  | 'NEEDS_REVIEW'
  | 'READY'
  | 'FAILED'

export type FoundryStage =
  | 'analyze'
  | 'reconstruct'
  | 'assemble'
  | 'generate-mjcf'
  | 'generate'
  | 'validate'

export interface FoundryProject {
  id: string
  project_id: string
  name: string
  description: string
  category: string
  status: FoundryStatus | string
  thumbnail_url: string | null
  thumbnail_asset_url: string | null
  metadata: Record<string, unknown>
  error_message: string | null
  created_at: string
  updated_at: string
  component_count: number
  joint_count: number
  actuated_joint_count: number
  latest_version: number | null
  validation_status: string | null
}

export interface FoundryImage {
  id: string
  project_id: string
  filename: string
  file_url: string
  asset_url: string
  image_type: string
  analysis_status: string
  width: number | null
  height: number | null
  created_at: string
}

export interface FoundryPhysics {
  mass_kg?: number
  inertia?: number[]
  center_of_mass?: number[]
  friction?: number
  estimated?: boolean
  user_confirmed?: boolean
  note?: string
}

export interface FoundryPlacement {
  body_pos?: number[]
  joint_pos?: number[]
  world_center?: number[]
}

export interface FoundryComponent {
  id: string
  project_id: string
  parent_id: string | null
  name: string
  label: string
  component_type: string
  mesh_url: string | null
  collision_mesh_url: string | null
  visual_asset_url: string | null
  collision_asset_url: string | null
  preview_asset_url: string | null
  metadata: {
    box?: number[]
    dimensions?: number[]
    physics?: FoundryPhysics
    placement?: FoundryPlacement
    mesh?: Record<string, unknown>
    segmentation_method?: string
    reconstruction_method?: string
    estimated?: boolean
    notes?: string[]
    [key: string]: unknown
  }
  confidence: number
  created_at: string
  updated_at: string
}

export interface FoundryJoint {
  id: string
  project_id: string
  component_id: string
  parent_component_id: string | null
  name: string
  joint_type: string
  axis: number[]
  position: number[]
  limits: number[] | null
  metadata: Record<string, unknown>
}

export interface FoundryActuator {
  id: string
  project_id: string
  joint_id: string
  name: string
  actuator_type: string
  configuration: {
    kp?: number
    kv?: number
    gear?: number
    ctrlrange?: number[]
    forcerange?: number[]
    estimated?: boolean
    [key: string]: unknown
  }
}

export interface FoundryJob {
  id: string
  project_id: string
  job_type: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | string
  progress: number
  message: string
  error_message: string | null
  result: Record<string, unknown> | null
  created_at: string
  completed_at: string | null
}

export interface FoundryVersion {
  id: string
  project_id: string
  version: number
  mjcf_url: string | null
  manifest_url: string | null
  validation: FoundryValidation | null
  created_at: string
}

export interface FoundryValidation {
  status: 'ok' | 'warning' | 'error' | string
  checks: Record<string, boolean>
  warnings: string[]
  errors: string[]
  details?: Record<string, unknown>
  validator_version?: string
  note?: string
}

export interface FoundryAnalysis {
  method?: string
  provider_configured?: boolean
  provider_error?: string | null
  note?: string
  image_size?: number[]
  foreground_box?: number[]
  scale_m_per_px?: number
  analyzed_at?: string
  [key: string]: unknown
}

export interface FoundryProjectDetail {
  project: FoundryProject
  images: FoundryImage[]
  components: FoundryComponent[]
  joints: FoundryJoint[]
  actuators: FoundryActuator[]
  jobs: FoundryJob[]
  versions: FoundryVersion[]
  analysis: FoundryAnalysis | null
}

export interface FoundrySimulateResult {
  foundry_id: string
  project_id: string
  version: number | null
  validation_status: string | null
  text_files: string[]
  binary_files: string[]
}

/** Success counts only with evaluator evidence behind it. */
export function hasEvaluatorSuccess(run: Pick<Run, 'status' | 'metrics' | 'evaluation'>): boolean {
  if (run.status !== 'succeeded') return false
  if (run.evaluation) return run.evaluation.success
  if (run.metrics) return run.metrics.success
  return false
}

/** Backend claims success but attached no evaluator evidence. */
export function isUnverifiedSuccess(run: Pick<Run, 'status' | 'metrics' | 'evaluation'>): boolean {
  return (
    run.status === 'succeeded' && run.metrics === null && run.evaluation === null
  )
}

/** Test pass rate for a run, or null when the backend ran no test suite. */
export function testPassRate(
  run: Pick<Run, 'metrics' | 'tests'>,
): { passed: number; total: number } | null {
  const fromMetrics = run.metrics
    ? { passed: run.metrics.tests_passed, total: run.metrics.tests_total }
    : null
  if (fromMetrics && fromMetrics.total > 0) return fromMetrics
  if (run.tests.length > 0) {
    return { passed: run.tests.filter((t) => t.passed).length, total: run.tests.length }
  }
  return fromMetrics
}

// ---- Model settings ----

/** Effective AI-model config. The API key itself is never returned. */
export interface ModelSettings {
  base_url: string
  model: string
  configured: boolean
  key_hint: string
  api_key_from_env: boolean
  base_url_from_env: boolean
  model_from_env: boolean
  persist_path: string
  persisted: boolean
}

/** Partial update: omitted fields stay unchanged, "" clears the key. */
export interface ModelSettingsUpdate {
  base_url?: string
  model?: string
  api_key?: string
}
