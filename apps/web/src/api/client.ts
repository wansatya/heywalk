// REST client for the HeyWalk backend (AGENTS.md section 10).
// Base defaults to same-origin "/api" (Vite dev proxy forwards to :8000).
// Set VITE_API_URL for a fully qualified backend origin in production builds.

import type {
  FixResult,
  FoundryActuator,
  FoundryComponent,
  FoundryJob,
  FoundryJoint,
  FoundryProject,
  FoundryProjectDetail,
  FoundrySimulateResult,
  FoundryStage,
  GenerationResult,
  RobotModel,
  ModelRequest,
  ModelSettings,
  ModelSettingsUpdate,
  Project,
  ProjectFileContent,
  ProjectFileEntry,
  RobotPackage,
  Run,
  RunComparison,
  StarterRobot,
  TelemetrySample,
  TestResult,
  TraceStep,
} from './types'

export const API_BASE: string =
  (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, '') ?? ''

export type ApiErrorKind = 'http' | 'network' | 'timeout'

export interface ApiErrorDetail {
  message: string
  hint?: string | null
  trace?: TraceStep[]
}

export class ApiError extends Error {
  kind: ApiErrorKind
  status: number | null
  /** Parsed structured body, when the backend sent one. */
  detail: ApiErrorDetail | null

  constructor(
    kind: ApiErrorKind,
    message: string,
    status: number | null = null,
    detail: ApiErrorDetail | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.status = status
    this.detail = detail
  }
}

/** Strip markup and cap length so a proxy/CDN error page never dumps raw HTML. */
function cleanTextBody(trimmed: string, fallback: string): string {
  const text = trimmed.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim()
  if (!text) return fallback
  return text.length > 300 ? `${text.slice(0, 300).trimEnd()}…` : text
}

/** Split an HTTP error body into a clean message plus optional structured detail. */
function parseErrorBody(body: string, status: number, path: string): { message: string; detail: ApiErrorDetail | null } {
  const fallback = `Request failed with HTTP ${status}: ${path}`
  const trimmed = body.trim()
  if (!trimmed) return { message: fallback, detail: null }
  try {
    const parsed: unknown = JSON.parse(trimmed)
    if (typeof parsed === 'object' && parsed !== null && 'detail' in parsed) {
      const raw = (parsed as { detail: unknown }).detail
      if (typeof raw === 'string') return { message: raw, detail: null }
      if (typeof raw === 'object' && raw !== null) {
        const obj = raw as Record<string, unknown>
        const message = typeof obj.message === 'string' && obj.message ? obj.message : fallback
        return {
          message,
          detail: {
            message,
            hint: typeof obj.hint === 'string' ? obj.hint : null,
            trace: Array.isArray(obj.trace) ? (obj.trace as TraceStep[]) : [],
          },
        }
      }
    }
    return { message: cleanTextBody(trimmed, fallback), detail: null }
  } catch {
    return { message: cleanTextBody(trimmed, fallback), detail: null }
  }
}

const DEFAULT_TIMEOUT_MS = 30_000
const MODEL_TIMEOUT_MS = 180_000

async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: controller.signal,
      headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
    })
  } catch (err) {
    window.clearTimeout(timer)
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError('timeout', `Request timed out after ${timeoutMs / 1000}s: ${path}`)
    }
    throw new ApiError('network', `Backend unreachable (${(err as Error).message}). Is the API server running?`)
  }
  window.clearTimeout(timer)

  if (!res.ok) {
    const body = await res.text().catch(() => '')
    const parsed = parseErrorBody(body, res.status, path)
    throw new ApiError('http', parsed.message, res.status, parsed.detail)
  }
  if (res.status === 204) return undefined as T
  const text = await res.text()
  if (!text) return undefined as T
  return JSON.parse(text) as T
}

function post<T>(path: string, body?: unknown, timeoutMs?: number): Promise<T> {
  return request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }, timeoutMs)
}

function put<T>(path: string, body: unknown, timeoutMs?: number): Promise<T> {
  return request<T>(path, { method: 'PUT', body: JSON.stringify(body) }, timeoutMs)
}

async function get<T>(path: string, timeoutMs?: number): Promise<T> {
  try {
    return await request<T>(path, { method: 'GET' }, timeoutMs)
  } catch (err) {
    // One retry for idempotent reads: on a slow link a GET can lose the
    // race for the browser's per-origin connections (bulk mesh downloads)
    // and hit the client timeout even though the server is healthy.
    if (err instanceof ApiError && err.kind === 'timeout') {
      return request<T>(path, { method: 'GET' }, timeoutMs)
    }
    throw err
  }
}

function del<T>(path: string, timeoutMs?: number): Promise<T> {
  return request<T>(path, { method: 'DELETE' }, timeoutMs)
}

// ---- Projects ----

export function listProjects(): Promise<Project[]> {
  return get<Project[]>('/api/projects')
}

export function createProject(name: string, task_description: string): Promise<Project> {
  return post<Project>('/api/projects', { name, task_description })
}

export function getProject(projectId: string): Promise<Project> {
  return get<Project>(`/api/projects/${projectId}`)
}

/** Delete a sequence with all its files, runs, and evidence. */
export function deleteProject(projectId: string): Promise<{ id: string; deleted: boolean }> {
  return del<{ id: string; deleted: boolean }>(`/api/projects/${projectId}`)
}

export function listProjectFiles(projectId: string): Promise<ProjectFileEntry[]> {
  return get<ProjectFileEntry[]>(`/api/projects/${projectId}/files`)
}

export function readProjectFile(projectId: string, path: string): Promise<ProjectFileContent> {
  return get<ProjectFileContent>(`/api/projects/${projectId}/files/${encodeURIComponent(path)}`)
}

export function saveProjectFile(
  projectId: string,
  path: string,
  content: string,
): Promise<ProjectFileContent> {
  return put<ProjectFileContent>(`/api/projects/${projectId}/files/${encodeURIComponent(path)}`, {
    content,
  })
}

export function exportProjectUrl(projectId: string): string {
  return `${API_BASE}/api/projects/${projectId}/export`
}

// ---- Generation ----

export function generateController(
  projectId: string,
  taskDescription?: string,
): Promise<GenerationResult> {
  return post<GenerationResult>(
    `/api/projects/${projectId}/generate`,
    taskDescription ? { task_description: taskDescription } : {},
    MODEL_TIMEOUT_MS,
  )
}

export function requestFix(projectId: string, runId: string): Promise<FixResult> {
  return post<FixResult>(
    `/api/projects/${projectId}/fix`,
    { run_id: runId },
    MODEL_TIMEOUT_MS,
  )
}

export function listModelRequests(projectId: string): Promise<ModelRequest[]> {
  return get<ModelRequest[]>(`/api/projects/${projectId}/model-requests`)
}

// ---- Model settings ----

export function getModelSettings(): Promise<ModelSettings> {
  return get<ModelSettings>('/api/settings/model')
}

export function updateModelSettings(patch: ModelSettingsUpdate): Promise<ModelSettings> {
  return put<ModelSettings>('/api/settings/model', patch)
}

// ---- Starter robots ----

/** Bundled robots the platform can install without a model request. */
export function listStarterRobots(): Promise<StarterRobot[]> {
  return get<StarterRobot[]>('/api/robots')
}

/** Write a bundled robot (MJCF + companions) into a project. */
export function installStarterRobot(
  projectId: string,
  robotId: string,
  includeController = false,
): Promise<RobotPackage> {
  return post<RobotPackage>(`/api/projects/${projectId}/robot`, {
    robot_id: robotId,
    include_controller: includeController,
  })
}

/**
 * Write the bundled sample controller (walk to the flag) and reset the
 * project to default grading. Returns the saved file.
 */
export function importSampleController(
  projectId: string,
  robotId: string,
): Promise<ProjectFileContent> {
  return post<ProjectFileContent>(`/api/projects/${projectId}/controller/sample`, {
    robot_id: robotId,
  })
}

/** Bundled robot id installed in a project (null = custom rig or none). */
export function getInstalledRobotId(projectId: string): Promise<string | null> {
  return get<{ robot_id: string | null }>(`/api/projects/${projectId}/robot`).then(
    (body) => body.robot_id,
  )
}

/** Bundled-robot model metadata + mesh-asset load status. */
export function getRobotModel(robotId: string): Promise<RobotModel> {
  return get<RobotModel>(`/api/robots/${encodeURIComponent(robotId)}/model`)
}

/** URL of one bundled robot mesh (STL) for the 3D view. */
export function robotAssetUrl(robotId: string, filename: string): string {
  return `${API_BASE}/api/robots/${encodeURIComponent(robotId)}/assets/${encodeURIComponent(filename)}`
}

/** URL of the single-response bundle with every mesh the 3D view needs. */
export function robotAssetBundleUrl(robotId: string): string {
  return `${API_BASE}/api/robots/${encodeURIComponent(robotId)}/assets/bundle`
}

// ---- Runs ----

export interface CreateRunOptions {
  world_config?: Record<string, unknown>
  seed?: number
}

export function createRun(projectId: string, options: CreateRunOptions = {}): Promise<Run> {
  return post<Run>(`/api/projects/${projectId}/runs`, options)
}

export function listRuns(projectId: string): Promise<Run[]> {
  return get<Run[]>(`/api/projects/${projectId}/runs`)
}

export function getRun(runId: string): Promise<Run> {
  return get<Run>(`/api/runs/${runId}`)
}

export function cancelRun(runId: string): Promise<void> {
  return post<void>(`/api/runs/${runId}/cancel`)
}

/** Start a fresh run of the same scenario (world + seed) as an earlier run.
 *  The new run records the rerun parent so before/after metrics compare. */
export function rerunRun(runId: string): Promise<Run> {
  return post<Run>(`/api/runs/${runId}/rerun`)
}

/** Latch the emergency stop for a live run. The backend may not implement
 *  this endpoint yet; callers should treat HTTP 404 as "unsupported". */
export function triggerEstop(runId: string): Promise<void> {
  return post<void>(`/api/runs/${runId}/estop`)
}

/** Suspend a live run's physics without losing state. */
export function pauseRun(runId: string): Promise<Run> {
  return post<Run>(`/api/runs/${runId}/pause`)
}

/** Resume a run suspended by pause. */
export function resumeRun(runId: string): Promise<Run> {
  return post<Run>(`/api/runs/${runId}/resume`)
}

export function getRunLogs(runId: string): Promise<string[]> {
  return get<string[]>(`/api/runs/${runId}/logs`)
}

export function getRunTelemetry(runId: string): Promise<TelemetrySample[]> {
  return get<TelemetrySample[]>(`/api/runs/${runId}/telemetry`)
}

/** Objective automated-test evidence for a MuJoCo run (AGENTS.md §8). */
export function getRunTests(runId: string): Promise<TestResult[]> {
  return get<TestResult[]>(`/api/runs/${runId}/tests`)
}

export function getRunComparison(runId: string, baselineRunId?: string | null): Promise<RunComparison> {
  const query = baselineRunId ? `?baseline_run_id=${encodeURIComponent(baselineRunId)}` : ''
  return get<RunComparison>(`/api/runs/${runId}/comparison${query}`)
}

export function streamUrl(runId: string): string {
  if (API_BASE.startsWith('http')) {
    const wsBase = API_BASE.replace(/^http/, 'ws')
    return `${wsBase}/api/runs/${runId}/stream`
  }
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/api/runs/${runId}/stream`
}

// ---- Robot Foundry --------------------------------------------------------

export function listFoundryProjects(): Promise<FoundryProject[]> {
  return get<FoundryProject[]>('/api/foundry/projects')
}

export function createFoundryProject(
  name: string,
  description: string,
  category: string,
): Promise<FoundryProject> {
  return post<FoundryProject>('/api/foundry/projects', { name, description, category })
}

export function getFoundryProject(foundryId: string): Promise<FoundryProjectDetail> {
  return get<FoundryProjectDetail>(`/api/foundry/projects/${foundryId}`)
}

export function getFoundryProjectForSequence(projectId: string): Promise<FoundryProject> {
  return get<FoundryProject>(`/api/foundry/by-sequence/${projectId}`)
}

export function updateFoundryProject(
  foundryId: string,
  patch: { name?: string; description?: string; category?: string },
): Promise<FoundryProject> {
  return request<FoundryProject>(`/api/foundry/projects/${foundryId}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })
}

export function deleteFoundryProject(
  foundryId: string,
): Promise<{ id: string; deleted: boolean }> {
  return del<{ id: string; deleted: boolean }>(`/api/foundry/projects/${foundryId}`)
}

/** Multipart upload; the shared JSON helper must not set Content-Type here. */
async function uploadFoundryImages(
  foundryId: string,
  files: File[],
  imageType: 'reference' | 'detail',
): Promise<unknown> {
  const form = new FormData()
  for (const file of files) form.append('files', file)
  form.append('image_type', imageType)
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 60_000)
  let response: Response
  try {
    response = await fetch(`${API_BASE}/api/foundry/projects/${foundryId}/images`, {
      method: 'POST',
      body: form,
      signal: controller.signal,
    })
  } catch (err) {
    window.clearTimeout(timer)
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError('timeout', 'Image upload timed out.')
    }
    throw new ApiError('network', `Backend unreachable (${(err as Error).message}).`)
  }
  window.clearTimeout(timer)
  if (!response.ok) {
    const body = await response.text().catch(() => '')
    const parsed = parseErrorBody(body, response.status, 'image upload')
    throw new ApiError('http', parsed.message, response.status, parsed.detail)
  }
  return response.json()
}

export { uploadFoundryImages }

export function createFoundryComponent(
  foundryId: string,
  body: {
    label: string
    component_type?: string
    parent_id?: string | null
    dimensions?: number[]
    confidence?: number
  },
): Promise<FoundryComponent> {
  return post<FoundryComponent>(`/api/foundry/projects/${foundryId}/components`, body)
}

export function deleteFoundryComponent(
  componentId: string,
): Promise<{ id: string; deleted: boolean }> {
  return del<{ id: string; deleted: boolean }>(`/api/foundry/components/${componentId}`)
}

export function updateFoundryComponent(
  componentId: string,
  patch: Record<string, unknown>,
): Promise<FoundryComponent> {
  return request<FoundryComponent>(`/api/foundry/components/${componentId}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })
}

export function updateFoundryJoint(
  jointId: string,
  patch: Record<string, unknown>,
): Promise<FoundryJoint> {
  return request<FoundryJoint>(`/api/foundry/joints/${jointId}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })
}

export function updateFoundryActuator(
  actuatorId: string,
  patch: Record<string, unknown>,
): Promise<FoundryActuator> {
  return request<FoundryActuator>(`/api/foundry/actuators/${actuatorId}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })
}

export function getFoundryJob(jobId: string): Promise<FoundryJob> {
  return get<FoundryJob>(`/api/foundry/jobs/${jobId}`)
}

export function startFoundryStage(
  foundryId: string,
  stage: FoundryStage,
): Promise<{ job: FoundryJob }> {
  return post<{ job: FoundryJob }>(`/api/foundry/projects/${foundryId}/${stage}`)
}

export function simulateFoundryProject(foundryId: string): Promise<FoundrySimulateResult> {
  return post<FoundrySimulateResult>(`/api/foundry/projects/${foundryId}/simulate`)
}

export function foundryAssetUrl(foundryId: string, assetPath: string | null | undefined): string | null {
  if (!assetPath) return null
  return `${API_BASE}/api/foundry/projects/${foundryId}/assets/${assetPath.replace(/^\/+/, '')}`
}

export function foundryModelUrl(foundryId: string): string {
  return `${API_BASE}/api/foundry/projects/${foundryId}/model`
}

export function foundryExportUrl(foundryId: string): string {
  return `${API_BASE}/api/foundry/projects/${foundryId}/export`
}

export function describeApiError(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return 'Unexpected error'
}

export function apiErrorDetail(err: unknown): ApiErrorDetail | null {
  if (err instanceof ApiError) return err.detail
  return null
}
