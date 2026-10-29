// REST client for the HeyWalk backend (AGENTS.md section 10).
// Base defaults to same-origin "/api" (Vite dev proxy forwards to :8000).
// Set VITE_API_URL for a fully qualified backend origin in production builds.

import type {
  FixResult,
  GenerationResult,
  MicroduckModel,
  ModelRequest,
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
    return { message: trimmed, detail: null }
  } catch {
    return { message: trimmed, detail: null }
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

/** Official MicroDuck model metadata + mesh-asset load status. */
export function getMicroduckModel(): Promise<MicroduckModel> {
  return get<MicroduckModel>('/api/robots/microduck/model')
}

/** URL of one vendored MicroDuck mesh (STL) for the 3D view. */
export function microduckAssetUrl(filename: string): string {
  return `${API_BASE}/api/robots/microduck/assets/${encodeURIComponent(filename)}`
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

export function describeApiError(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return 'Unexpected error'
}

export function apiErrorDetail(err: unknown): ApiErrorDetail | null {
  if (err instanceof ApiError) return err.detail
  return null
}
