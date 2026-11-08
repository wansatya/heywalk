import { create } from 'zustand'
import {
  createFoundryComponent,
  createFoundryProject,
  deleteFoundryComponent,
  deleteFoundryProject,
  describeApiError,
  getFoundryJob,
  getFoundryProject,
  listFoundryProjects,
  simulateFoundryProject,
  startFoundryStage,
  updateFoundryActuator,
  updateFoundryComponent,
  updateFoundryJoint,
  uploadFoundryImages,
} from '../api/client'
import type {
  FoundryJob,
  FoundryProject,
  FoundryProjectDetail,
  FoundrySimulateResult,
  FoundryStage,
} from '../api/types'
import { useWorkspaceStore } from './workspace'

type AsyncStatus = 'idle' | 'loading' | 'success' | 'error'

interface AsyncState<T> {
  status: AsyncStatus
  error: string | null
  data: T | null
}

function idle<T>(): AsyncState<T> {
  return { status: 'idle', error: null, data: null }
}

function loading<T>(prev: AsyncState<T>): AsyncState<T> {
  return { status: 'loading', error: null, data: prev.data }
}

/** Stages shown in the Foundry workspace, in pipeline order. */
export const FOUNDRY_STAGES: Array<{ id: FoundryStage; label: string; hint: string }> = [
  { id: 'analyze', label: 'Analyze image', hint: 'Segment the reference into reviewable parts.' },
  { id: 'reconstruct', label: 'Reconstruct 3D', hint: 'Turn each mask into real OBJ/STL/GLB meshes.' },
  { id: 'assemble', label: 'Assemble', hint: 'Infer the hierarchy, joints, and estimated physics.' },
  { id: 'generate-mjcf', label: 'Generate MJCF', hint: 'Build robot.xml and validate it with MuJoCo.' },
  { id: 'validate', label: 'Re-validate', hint: 'Re-run the static checks and MuJoCo compile.' },
]

const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))

/** Poll a stage job until it stops; returns the final job row. */
async function pollJob(jobId: string): Promise<FoundryJob> {
  const deadline = Date.now() + 10 * 60 * 1000
  while (Date.now() < deadline) {
    const job = await getFoundryJob(jobId)
    if (job.status === 'succeeded' || job.status === 'failed') return job
    await sleep(450)
  }
  throw new Error('The Foundry job did not finish within 10 minutes.')
}

interface FoundryState {
  open: boolean
  projects: FoundryProject[]
  projectsState: AsyncState<FoundryProject[]>
  activeId: string | null
  detail: FoundryProjectDetail | null
  detailStatus: AsyncStatus
  detailError: string | null
  job: FoundryJob | null
  activeStage: FoundryStage | null
  actionError: string | null
  uploading: boolean
  saving: boolean
  simulating: boolean
  simulateResult: FoundrySimulateResult | null

  setOpen: (open: boolean) => void
  refreshProjects: () => Promise<void>
  createProject: (name: string, description: string, category: string) => Promise<FoundryProject | null>
  selectProject: (id: string | null) => Promise<void>
  refreshDetail: () => Promise<void>
  deleteProject: (id: string) => Promise<void>
  uploadImages: (files: File[], imageType: 'reference' | 'detail') => Promise<void>
  runStage: (stage: FoundryStage) => Promise<void>
  addComponent: (label: string) => Promise<void>
  updateComponent: (componentId: string, patch: Record<string, unknown>) => Promise<void>
  deleteComponent: (componentId: string) => Promise<void>
  updateJoint: (jointId: string, patch: Record<string, unknown>) => Promise<void>
  updateActuator: (actuatorId: string, patch: Record<string, unknown>) => Promise<void>
  simulate: () => Promise<FoundrySimulateResult | null>
  clearActionError: () => void
}

let pollToken = 0

export const useFoundryStore = create<FoundryState>((set, get) => ({
  open: false,
  projects: [],
  projectsState: idle(),
  activeId: null,
  detail: null,
  detailStatus: 'idle',
  detailError: null,
  job: null,
  activeStage: null,
  actionError: null,
  uploading: false,
  saving: false,
  simulating: false,
  simulateResult: null,

  setOpen: (open) => {
    set({ open })
    if (open) void get().refreshProjects()
  },

  refreshProjects: async () => {
    set({ projectsState: loading(get().projectsState) })
    try {
      const projects = await listFoundryProjects()
      set({ projects, projectsState: { status: 'success', error: null, data: projects } })
    } catch (err) {
      set({
        projectsState: { status: 'error', error: describeApiError(err), data: get().projects },
      })
    }
  },

  createProject: async (name, description, category) => {
    set({ actionError: null })
    try {
      const project = await createFoundryProject(name, description, category)
      set((state) => ({
        projects: [project, ...state.projects],
        activeId: project.id,
        detail: null,
        detailStatus: 'idle',
        job: null,
        activeStage: null,
        simulateResult: null,
      }))
      // The Foundry creates a normal sequence too; keep the IDE list fresh so
      // "Open in Simulator" can select it without a page reload.
      void useWorkspaceStore.getState().refreshProjects().catch(() => undefined)
      await get().refreshDetail()
      return project
    } catch (err) {
      set({ actionError: describeApiError(err) })
      return null
    }
  },

  selectProject: async (id) => {
    pollToken += 1
    set({
      activeId: id,
      detail: null,
      detailStatus: id ? 'loading' : 'idle',
      detailError: null,
      job: null,
      activeStage: null,
      actionError: null,
      simulateResult: null,
    })
    if (!id) return
    try {
      const detail = await getFoundryProject(id)
      if (get().activeId !== id) return
      set({ detail, detailStatus: 'success' })
    } catch (err) {
      if (get().activeId !== id) return
      set({ detailStatus: 'error', detailError: describeApiError(err) })
    }
  },

  refreshDetail: async () => {
    const { activeId } = get()
    if (!activeId) return
    try {
      const detail = await getFoundryProject(activeId)
      if (get().activeId !== activeId) return
      set({ detail, detailStatus: 'success', detailError: null })
      set((state) => ({
        projects: state.projects.map((project) =>
          project.id === detail.project.id ? detail.project : project,
        ),
      }))
    } catch (err) {
      set({ detailStatus: 'error', detailError: describeApiError(err) })
    }
  },

  deleteProject: async (id) => {
    set({ actionError: null })
    try {
      await deleteFoundryProject(id)
      set((state) => ({
        projects: state.projects.filter((project) => project.id !== id),
        activeId: state.activeId === id ? null : state.activeId,
        detail: state.activeId === id ? null : state.detail,
        detailStatus: state.activeId === id ? 'idle' : state.detailStatus,
      }))
      void useWorkspaceStore.getState().refreshProjects().catch(() => undefined)
    } catch (err) {
      set({ actionError: describeApiError(err) })
    }
  },

  uploadImages: async (files, imageType) => {
    const { activeId } = get()
    if (!activeId || files.length === 0) return
    set({ uploading: true, actionError: null })
    try {
      await uploadFoundryImages(activeId, files, imageType)
      await get().refreshDetail()
    } catch (err) {
      set({ actionError: describeApiError(err) })
    } finally {
      set({ uploading: false })
    }
  },

  runStage: async (stage) => {
    const { activeId } = get()
    if (!activeId) return
    pollToken += 1
    const token = pollToken
    set({ activeStage: stage, actionError: null, simulateResult: null })
    try {
      const { job } = await startFoundryStage(activeId, stage)
      set({ job })
      const finished = await pollJob(job.id)
      if (token !== pollToken) return
      set({ job: finished })
      if (finished.status === 'failed') {
        set({ actionError: finished.error_message ?? 'The Foundry stage failed.' })
      }
      await get().refreshDetail()
      await get().refreshProjects()
    } catch (err) {
      if (token === pollToken) set({ actionError: describeApiError(err) })
    } finally {
      if (token === pollToken) set({ activeStage: null })
    }
  },

  addComponent: async (label) => {
    const { activeId } = get()
    if (!activeId || !label.trim()) return
    set({ saving: true, actionError: null })
    try {
      await createFoundryComponent(activeId, { label: label.trim() })
      await get().refreshDetail()
    } catch (err) {
      set({ actionError: describeApiError(err) })
    } finally {
      set({ saving: false })
    }
  },

  updateComponent: async (componentId, patch) => {
    set({ saving: true, actionError: null })
    try {
      await updateFoundryComponent(componentId, patch)
      await get().refreshDetail()
    } catch (err) {
      set({ actionError: describeApiError(err) })
    } finally {
      set({ saving: false })
    }
  },

  deleteComponent: async (componentId) => {
    set({ saving: true, actionError: null })
    try {
      await deleteFoundryComponent(componentId)
      await get().refreshDetail()
    } catch (err) {
      set({ actionError: describeApiError(err) })
    } finally {
      set({ saving: false })
    }
  },

  updateJoint: async (jointId, patch) => {
    set({ saving: true, actionError: null })
    try {
      await updateFoundryJoint(jointId, patch)
      await get().refreshDetail()
    } catch (err) {
      set({ actionError: describeApiError(err) })
    } finally {
      set({ saving: false })
    }
  },

  updateActuator: async (actuatorId, patch) => {
    set({ saving: true, actionError: null })
    try {
      await updateFoundryActuator(actuatorId, patch)
      await get().refreshDetail()
    } catch (err) {
      set({ actionError: describeApiError(err) })
    } finally {
      set({ saving: false })
    }
  },

  simulate: async () => {
    const { activeId } = get()
    if (!activeId) return null
    set({ simulating: true, actionError: null })
    try {
      const result = await simulateFoundryProject(activeId)
      set({ simulateResult: result })
      const workspace = useWorkspaceStore.getState()
      await workspace.refreshProjects()
      // The backend creates a normal sequence and installs the robot into it
      // only at this explicit "open in simulator" step.
      await workspace.selectProject(result.project_id)
      return result
    } catch (err) {
      set({ actionError: describeApiError(err) })
      return null
    } finally {
      set({ simulating: false })
    }
  },

  clearActionError: () => set({ actionError: null }),
}))
