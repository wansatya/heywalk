import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import {
  apiErrorDetail,
  createProject,
  deleteProject as deleteProjectApi,
  describeApiError,
  generateController,
  getFoundryProjectForSequence,
  getInstalledRobotId,
  getProject,
  getRobotModel,
  importSampleController,
  installStarterRobot,
  listModelRequests,
  listProjectFiles,
  listProjects,
  listStarterRobots,
  readProjectFile,
  requestFix,
  saveProjectFile,
} from '../api/client'
import type {
  FixResult,
  GenerationResult,
  ModelRequest,
  Project,
  ProjectFileEntry,
  RobotModel,
  RobotPackage,
  RunObjective,
  StarterRobot,
  TraceStep,
} from '../api/types'

export type CenterTab = 'simulator' | 'code'

/** Fallback robot: fresh projects auto-install MicroDuck; the user can switch. */
export const DEFAULT_ROBOT_ID = 'microduck'

/** Entry point the simulator executes; Start runs its saved content. */
export const CONTROLLER_PATH = 'controller.py'

/** Normalize a project/generation payload to a phase list (legacy OK). */
function normalizeObjectives(source: {
  objectives?: RunObjective[] | null
  objective?: RunObjective | null
}): RunObjective[] {
  if (source.objectives) return source.objectives
  if (source.objective) return [source.objective]
  return []
}

export type AsyncStatus = 'idle' | 'loading' | 'success' | 'error'

export interface AsyncState<T> {
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

interface WorkspaceState {
  projects: Project[]
  activeProjectId: string | null
  taskDraft: string
  files: ProjectFileEntry[]
  activeFilePath: string
  /** Open editor tabs, in the order they were opened. */
  openPaths: string[]
  editorContent: string
  editorDirty: boolean
  editorVersion: number | null
  /** True when controller.py is blank (null = missing/unknown); gates Start. */
  controllerBlank: boolean | null
  /** Sample-script import state (walk to the flag). */
  sampleState: AsyncState<null>

  projectsState: AsyncState<Project[]>
  filesState: AsyncStatus
  filesError: string | null
  saveState: AsyncState<number> // data = saved version
  generation: AsyncState<GenerationResult>
  fix: AsyncState<FixResult>
  fixRunId: string | null
  /** Pre-fix file contents (null = file did not exist) for the code diff. */
  fixBase: Record<string, string | null>
  /** Pre-AI-update snapshot for editor diff highlighting (green/red). */
  diffBase: { path: string; content: string } | null
  /** Phase sequence for the current controller (empty = go to the flag). */
  objectives: RunObjective[]
  /** Under-the-hood stage trace + recovery hint for the last generate/fix. */
  generationTrace: TraceStep[]
  generationHint: string | null
  fixTrace: TraceStep[]
  fixHint: string | null
  /** Recent model-request log rows (model, latency, prompt version, status). */
  modelRequests: ModelRequest[]
  /** Starter-robot install state (auto-install or modal switch). */
  robotInstall: AsyncState<RobotPackage>
  /** Projects already attempted this session (no install-retry loops). */
  autoInstallTried: Record<string, boolean>
  /** Bundled robot installed in the active project (null = custom/unknown). */
  installedRobotId: string | null
  /** True when the active sequence is a Robot Foundry generated robot. */
  isFoundryProject: boolean
  /** Foundry id linked to the active sequence, if any. */
  foundryProjectId: string | null
  /** Switch-robot modal catalog + visibility. */
  robotCatalog: AsyncState<StarterRobot[]>
  robotModalOpen: boolean
  /** Installed robot's model metadata + mesh-asset load status. */
  robotModel: AsyncState<RobotModel>
  /** Middle-column top tab; opening a file switches to the editor. */
  centerTab: CenterTab

  refreshProjects: () => Promise<void>
  selectProject: (id: string | null) => Promise<void>
  newExperiment: (name?: string, robotId?: string) => Promise<void>
  /** Delete a sequence; returns the project id to show next, if any. */
  deleteProject: (id: string) => Promise<string | null>
  setTaskDraft: (task: string) => void

  refreshFiles: () => Promise<void>
  openFile: (path: string) => Promise<void>
  closeFile: (path: string) => void
  editContent: (content: string) => void
  saveActiveFile: () => Promise<void>
  /** Load the bundled sample controller into the editor (unsaved). */
  importSample: () => Promise<void>

  generate: () => Promise<void>
  fixFromRun: (runId: string) => Promise<void>
  dismissFix: () => void
  refreshModelRequests: () => Promise<void>
  installRobot: (robotId: string) => Promise<void>
  refreshRobotModel: (robotId: string) => Promise<void>
  refreshRobotCatalog: () => Promise<void>
  setRobotModalOpen: (open: boolean) => void
  setCenterTab: (tab: CenterTab) => void
}

function activeProjectIdOf(projects: Project[], preferred: string | null): string | null {
  if (preferred && projects.some((p) => p.id === preferred)) return preferred
  return projects[0]?.id ?? null
}

export const useWorkspaceStore = create<WorkspaceState>()(
  persist(
    (set, get) => ({
      projects: [],
      activeProjectId: null,
      taskDraft: '',
      files: [],
      activeFilePath: '',
      openPaths: [],
      editorContent: '',
      editorDirty: false,
      editorVersion: null,
      controllerBlank: null,
      sampleState: idle(),
      objectives: [],

      projectsState: idle(),
      filesState: 'idle',
      filesError: null,
      saveState: idle(),
      generation: idle(),
      fix: idle(),
      fixRunId: null,
      fixBase: {},
      diffBase: null,
      generationTrace: [],
      generationHint: null,
      fixTrace: [],
      fixHint: null,
      modelRequests: [],
      robotInstall: idle(),
      autoInstallTried: {},
      installedRobotId: null,
      isFoundryProject: false,
      foundryProjectId: null,
      robotCatalog: idle(),
      robotModalOpen: false,
      robotModel: idle(),
      centerTab: 'simulator',

      refreshProjects: async () => {
        set({ projectsState: loading(get().projectsState) })
        try {
          const projects = await listProjects()
          const activeProjectId = activeProjectIdOf(projects, get().activeProjectId)
          set({ projects, activeProjectId, projectsState: { status: 'success', error: null, data: projects } })
          if (activeProjectId) {
            const project = projects.find((p) => p.id === activeProjectId)
            // The task text belongs to the sequence, so a sequence without one
            // must leave the field empty (Start stays disabled until it is
            // written); a draft is never inherited from another sequence.
            if (project) set({ taskDraft: project.task_description ?? '' })
            // The controller's phases travel with the project; only
            // sync when the backend serves them (older backends omit the
            // key, in which case a just-generated value must survive).
            if (project && ('objectives' in project || 'objective' in project)) {
              set({ objectives: normalizeObjectives(project) })
            }
            await get().refreshFiles()
          } else {
            set({
              files: [],
              activeFilePath: '',
              openPaths: [],
              editorContent: '',
              editorDirty: false,
              controllerBlank: null,
              isFoundryProject: false,
              foundryProjectId: null,
            })
          }
        } catch (err) {
          set({
            projectsState: { status: 'error', error: describeApiError(err), data: get().projects },
          })
        }
      },

      selectProject: async (id) => {
        set({
          activeProjectId: id,
          files: [],
          activeFilePath: '',
          openPaths: [],
          editorContent: '',
          editorDirty: false,
          editorVersion: null,
          controllerBlank: null,
          sampleState: idle(),
          generation: idle(),
          fix: idle(),
          fixRunId: null,
          fixBase: {},
          diffBase: null,
          objectives: [],
          generationTrace: [],
          generationHint: null,
          fixTrace: [],
          fixHint: null,
          modelRequests: [],
          robotInstall: idle(),
          installedRobotId: null,
          isFoundryProject: false,
          foundryProjectId: null,
          robotModel: idle(),
          saveState: idle(),
        })
        if (!id) return
        try {
          const project = await getProject(id)
          set({ taskDraft: project.task_description ?? '' })
          if ('objectives' in project || 'objective' in project) {
            set({ objectives: normalizeObjectives(project) })
          }
        } catch {
          // Keep the draft; project metadata is non-critical here.
        }
        await get().refreshFiles()
        await get().refreshModelRequests()
      },

      newExperiment: async (name, robotId) => {
        // A new sequence starts without a task: nothing is generated and no
        // runner is started. A robot is installed below so the simulator can
        // show it straight away, while Start waits for the task text.
        const projectName = name?.trim() || `Sequence ${get().projects.length + 1}`
        const project = await createProject(projectName, '')
        set((state) => ({
          projects: [project, ...state.projects],
          activeProjectId: project.id,
          taskDraft: project.task_description,
          files: [],
          activeFilePath: '',
          openPaths: [],
          editorContent: '',
          editorDirty: false,
          editorVersion: null,
          controllerBlank: null,
          sampleState: idle(),
          generation: idle(),
          fix: idle(),
          fixRunId: null,
          fixBase: {},
          diffBase: null,
          objectives: [],
          generationTrace: [],
          generationHint: null,
          fixTrace: [],
          fixHint: null,
          modelRequests: [],
          robotInstall: idle(),
          installedRobotId: null,
          isFoundryProject: false,
          foundryProjectId: null,
          robotModel: idle(),
          // A caller-selected robot installs explicitly below. Mark the
          // project as attempted so refreshFiles cannot auto-install
          // MicroDuck first.
          autoInstallTried: robotId
            ? { ...state.autoInstallTried, [project.id]: true }
            : state.autoInstallTried,
        }))
        if (robotId) {
          await get().installRobot(robotId)
        } else {
          // A fresh sequence starts with MicroDuck installed (auto-install
          // runs inside refreshFiles when the file list comes back empty).
          await get().refreshFiles().catch(() => undefined)
        }
        set({ centerTab: 'simulator' })
      },

      deleteProject: async (id) => {
        await deleteProjectApi(id)
        const projects = get().projects.filter((p) => p.id !== id)
        const next = get().activeProjectId === id ? (projects[0]?.id ?? null) : get().activeProjectId
        set({ projects })
        if (get().activeProjectId === id) await get().selectProject(next)
        return next
      },

      setTaskDraft: (task) => set({ taskDraft: task }),

      refreshFiles: async () => {
        const { activeProjectId } = get()
        if (!activeProjectId) return
        set({ filesState: 'loading', filesError: null })
        try {
          const files = await listProjectFiles(activeProjectId)
          set({ files, filesState: 'success' })
          // Track controller blankness for Start gating (without
          // disturbing the open editor; a dirty buffer wins below).
          if (files.some((f) => f.path === CONTROLLER_PATH)) {
            if (get().activeFilePath !== CONTROLLER_PATH || !get().editorDirty) {
              try {
                const controller = await readProjectFile(activeProjectId, CONTROLLER_PATH)
                if (get().activeFilePath !== CONTROLLER_PATH || !get().editorDirty) {
                  set({ controllerBlank: controller.content.trim() === '' })
                }
              } catch {
                set({ controllerBlank: null })
              }
            }
          } else {
            set({ controllerBlank: null })
          }
          if (files.length === 0 && !get().autoInstallTried[activeProjectId]) {
            // Fresh sequence: install the default robot without asking so
            // the simulator has something to show. One attempt per project
            // per session; a failure surfaces in the explorer with the
            // switch-robot modal as the manual retry.
            set((state) => ({
              autoInstallTried: { ...state.autoInstallTried, [activeProjectId]: true },
            }))
            await get().installRobot(DEFAULT_ROBOT_ID)
            return
          }
          if (files.length > 0) {
            // Track which bundled robot (if any) the file list holds, so
            // the 3D view and the sample import follow project switches.
            // Best-effort: a failure leaves the previous value, never a
            // spinner — the file list itself already loaded.
            try {
              set({ installedRobotId: await getInstalledRobotId(activeProjectId) })
            } catch {
              // Keep the previous value; the explorer retry re-runs this.
            }
            // A Foundry-generated robot is a custom rig: the simulator must
            // not fall back to the bundled MicroDuck preview.
            try {
              const foundry = await getFoundryProjectForSequence(activeProjectId)
              set({ isFoundryProject: true, foundryProjectId: foundry.id })
            } catch {
              set({ isFoundryProject: false, foundryProjectId: null })
            }
          } else {
            set({ isFoundryProject: false, foundryProjectId: null })
          }
          const { activeFilePath } = get()
          if (files.length > 0 && !files.some((f) => f.path === activeFilePath)) {
            const preferred =
              files.find((f) => f.path.endsWith('controller.py'))?.path ?? files[0].path
            await get().openFile(preferred)
          } else if (files.length === 0) {
            set({ activeFilePath: '', openPaths: [], editorContent: '', editorDirty: false, editorVersion: null })
          }
        } catch (err) {
          set({ filesState: 'error', filesError: describeApiError(err) })
        }
      },

      setCenterTab: (tab) => set({ centerTab: tab }),

      openFile: async (path) => {
        const { activeProjectId } = get()
        if (!activeProjectId) return
        set((state) => ({
          centerTab: 'code',
          filesState: 'loading',
          filesError: null,
          openPaths: state.openPaths.includes(path) ? state.openPaths : [...state.openPaths, path],
        }))
        try {
          const file = await readProjectFile(activeProjectId, path)
          set({
            activeFilePath: file.path,
            editorContent: file.content,
            editorDirty: false,
            editorVersion: file.version,
            filesState: 'success',
          })
          if (file.path === CONTROLLER_PATH) {
            set({ controllerBlank: file.content.trim() === '' })
          }
        } catch (err) {
          set((state) => ({
            filesState: 'error',
            filesError: describeApiError(err),
            openPaths: state.openPaths.filter((p) => p !== path),
          }))
        }
      },

      closeFile: (path) => {
        const current = get().openPaths
        const openPaths = current.filter((p) => p !== path)
        set({ openPaths })
        if (get().activeFilePath !== path) return
        // Activate the left neighbor, like an IDE; with no tabs left,
        // fall back to the simulator.
        const idx = current.indexOf(path)
        const next = (idx > 0 ? current[idx - 1] : openPaths[0]) ?? null
        if (next) void get().openFile(next)
        else {
          set({
            activeFilePath: '',
            editorContent: '',
            editorDirty: false,
            editorVersion: null,
            centerTab: 'simulator',
          })
        }
      },

      editContent: (content) => {
        const patch: { editorContent: string; editorDirty: boolean; saveState: AsyncState<number>; controllerBlank?: boolean | null } = {
          editorContent: content,
          editorDirty: true,
          saveState: idle(),
        }
        if (get().activeFilePath === CONTROLLER_PATH) {
          patch.controllerBlank = content.trim() === ''
        }
        set(patch)
      },

      saveActiveFile: async () => {
        const { activeProjectId, activeFilePath, editorContent } = get()
        if (!activeProjectId || !activeFilePath) return
        set({ saveState: loading(get().saveState) })
        try {
          const saved = await saveProjectFile(activeProjectId, activeFilePath, editorContent)
          set({
            editorDirty: false,
            editorVersion: saved.version,
            saveState: { status: 'success', error: null, data: saved.version },
          })
          await get().refreshFiles()
        } catch (err) {
          set({ saveState: { status: 'error', error: describeApiError(err), data: null } })
        }
      },

      generate: async () => {
        const { activeProjectId, taskDraft } = get()
        if (!activeProjectId) return
        if (!taskDraft.trim()) {
          // The controller is generated from the task; an empty prompt would
          // produce code for no objective and could still be run.
          set({ generation: { status: 'error', error: 'Describe the task first: the controller is generated from it.', data: null } })
          return
        }
        // Snapshot current contents first: the backend writes the generated
        // files on success, so the pre-generate text is the diff baseline.
        const base: Record<string, string | null> = {}
        for (const entry of get().files) {
          try {
            const file = await readProjectFile(activeProjectId, entry.path)
            base[file.path] = file.content
          } catch {
            base[entry.path] = null
          }
        }
        set({ generation: loading(get().generation), generationTrace: [], generationHint: null })
        try {
          const result = await generateController(activeProjectId, taskDraft)
          set({
            generation: { status: 'success', error: null, data: result },
            generationTrace: result.trace ?? [],
            objectives: normalizeObjectives(result),
          })
          await get().refreshModelRequests()
          await get().refreshFiles()
          const first = result.files[0]?.path
          if (first) {
            await get().openFile(first)
            set({ diffBase: { path: first, content: base[first] ?? '' } })
          }
        } catch (err) {
          const detail = apiErrorDetail(err)
          set({
            generation: { status: 'error', error: describeApiError(err), data: null },
            generationTrace: detail?.trace ?? [],
            generationHint: detail?.hint ?? null,
          })
          await get().refreshModelRequests()
        }
      },

      fixFromRun: async (runId) => {
        const { activeProjectId } = get()
        if (!activeProjectId) return
        // Snapshot current contents first: the backend writes the validated
        // correction on success, so the pre-fix text is the diff baseline.
        const base: Record<string, string | null> = {}
        for (const entry of get().files) {
          try {
            const file = await readProjectFile(activeProjectId, entry.path)
            base[file.path] = file.content
          } catch {
            base[entry.path] = null
          }
        }
        set({ fix: loading(get().fix), fixRunId: runId, fixBase: base, fixTrace: [], fixHint: null })
        try {
          const result = await requestFix(activeProjectId, runId)
          set({ fix: { status: 'success', error: null, data: result }, fixTrace: result.trace ?? [] })
          // The backend already applied the validated correction; refresh so
          // the editor shows exactly what will execute on rerun.
          await get().refreshFiles()
          const first = result.updated_files[0]?.path
          if (first) {
            await get().openFile(first)
            set({ diffBase: { path: first, content: base[first] ?? '' } })
          }
          await get().refreshModelRequests()
        } catch (err) {
          const detail = apiErrorDetail(err)
          set({
            fix: { status: 'error', error: describeApiError(err), data: null },
            fixTrace: detail?.trace ?? [],
            fixHint: detail?.hint ?? null,
          })
          await get().refreshModelRequests()
        }
      },

      installRobot: async (robotId) => {
        const { activeProjectId } = get()
        if (!activeProjectId) return
        set({ robotInstall: loading(get().robotInstall) })
        try {
          // Robots install with an EMPTY controller; the sample comes via
          // importSample and generation writes fresh code — installs never
          // clobber user code.
          const robot = await installStarterRobot(activeProjectId, robotId, false)
          set({
            robotInstall: { status: 'success', error: null, data: robot },
            installedRobotId: robotId,
            isFoundryProject: false,
            foundryProjectId: null,
          })
          await get().refreshFiles()
          await get().refreshRobotModel(robotId)
          // Fresh sequences land on the empty controller with its
          // import-sample / generate next steps.
          const paths = get().files.map((f) => f.path)
          await get().openFile(paths.includes(CONTROLLER_PATH) ? CONTROLLER_PATH : robot.model_path)
        } catch (err) {
          set({
            robotInstall: { status: 'error', error: describeApiError(err), data: null },
          })
        }
      },

      importSample: async () => {
        const { activeProjectId } = get()
        if (!activeProjectId) return
        set({ sampleState: loading(get().sampleState) })
        try {
          // Server-side and atomic: writes that robot's sample and resets
          // the project to default grading in one step.
          const saved = await importSampleController(
            activeProjectId,
            get().installedRobotId ?? DEFAULT_ROBOT_ID,
          )
          // The sample is the walk-to-flag behavior, so the backend also
          // writes its matching task. Read it back so Start is enabled on a
          // fresh sequence whose task was empty before the import.
          let taskDraft = get().taskDraft
          try {
            const project = await getProject(activeProjectId)
            taskDraft = project.task_description ?? ''
          } catch {
            // The sample was written; a metadata read failure must not undo it.
          }
          set({ taskDraft, objectives: [], diffBase: null })
          await get().refreshFiles()
          await get().openFile(saved.path)
          set({ sampleState: { status: 'success', error: null, data: null } })
        } catch (err) {
          set({ sampleState: { status: 'error', error: describeApiError(err), data: null } })
        }
      },

      refreshRobotModel: async (robotId) => {
        set({ robotModel: loading(get().robotModel) })
        try {
          const model = await getRobotModel(robotId)
          if (!model.assets_ok) {
            set({
              robotModel: {
                status: 'error',
                error: model.error ?? 'Robot mesh assets are missing.',
                data: model,
              },
            })
          } else {
            set({ robotModel: { status: 'success', error: null, data: model } })
          }
        } catch (err) {
          set({
            robotModel: { status: 'error', error: describeApiError(err), data: null },
          })
        }
      },

      refreshRobotCatalog: async () => {
        set({ robotCatalog: loading(get().robotCatalog) })
        try {
          const robots = await listStarterRobots()
          set({ robotCatalog: { status: 'success', error: null, data: robots } })
        } catch (err) {
          set({
            robotCatalog: { status: 'error', error: describeApiError(err), data: null },
          })
        }
      },

      setRobotModalOpen: (open) => {
        set({ robotModalOpen: open })
        if (open) void get().refreshRobotCatalog()
      },

      dismissFix: () => set({ fix: idle(), fixRunId: null, fixBase: {}, diffBase: null, fixTrace: [], fixHint: null }),

      refreshModelRequests: async () => {
        const { activeProjectId } = get()
        if (!activeProjectId) {
          set({ modelRequests: [] })
          return
        }
        try {
          set({ modelRequests: await listModelRequests(activeProjectId) })
        } catch {
          // Non-critical; the trace on the operation itself carries the detail.
        }
      },
    }),
    {
      name: 'heywalk-workspace',
      partialize: (state) => ({ activeProjectId: state.activeProjectId }) as WorkspaceState,
    },
  ),
)
