import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowLeft,
  Box,
  CheckCircle2,
  Download,
  ImagePlus,
  Loader2,
  Play,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
} from 'lucide-react'
import { FOUNDRY_STAGES, useFoundryStore } from '../../stores/foundry'
import { useWorkspaceStore } from '../../stores/workspace'
import { foundryExportUrl } from '../../api/client'
import type {
  FoundryActuator,
  FoundryComponent,
  FoundryJoint,
  FoundryValidation,
} from '../../api/types'
import { ActionButton, ErrorNotice, LoadingRow } from '../../components/Panel'
import { clsx } from '../../components/clsx'
import { FoundryViewport } from './FoundryViewport'

const COMPONENT_TYPES = ['torso', 'head', 'arm', 'leg', 'wheel', 'base', 'other']
const JOINT_TYPES = ['fixed', 'hinge', 'slide']

function percent(value: number): string {
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`
}

/** Flatten the component tree in parent-before-child order for the list. */
function hierarchyRows(
  components: FoundryComponent[],
): Array<{ component: FoundryComponent; depth: number }> {
  const byParent = new Map<string | null, FoundryComponent[]>()
  const ids = new Set(components.map((component) => component.id))
  for (const component of components) {
    const parent = component.parent_id && ids.has(component.parent_id) ? component.parent_id : null
    const siblings = byParent.get(parent) ?? []
    siblings.push(component)
    byParent.set(parent, siblings)
  }
  const rows: Array<{ component: FoundryComponent; depth: number }> = []
  const seen = new Set<string>()
  const walk = (parent: string | null, depth: number) => {
    for (const component of byParent.get(parent) ?? []) {
      if (seen.has(component.id)) continue
      seen.add(component.id)
      rows.push({ component, depth })
      walk(component.id, depth + 1)
    }
  }
  walk(null, 0)
  // A corrupt cycle has no root; still show every part exactly once.
  for (const component of components) {
    if (!seen.has(component.id)) rows.push({ component, depth: 0 })
  }
  return rows
}

function NumberField({
  label,
  value,
  step = 0.01,
  min,
  onCommit,
  suffix,
}: {
  label: string
  value: number | undefined
  step?: number
  min?: number
  onCommit: (value: number) => void
  suffix?: string
}) {
  const [draft, setDraft] = useState(value === undefined ? '' : String(value))
  useEffect(() => setDraft(value === undefined ? '' : String(value)), [value])
  return (
    <label className="flex flex-col gap-1 text-[11px] font-medium text-stone-500">
      {label}
      <span className="relative">
        <input
          type="number"
          step={step}
          min={min}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={() => {
            const parsed = Number(draft)
            if (Number.isFinite(parsed) && parsed !== value) onCommit(parsed)
          }}
          className="w-full rounded-lg border border-border-subtle bg-base px-2.5 py-1.5 pr-9 text-xs text-stone-800 outline-none focus:border-amber-500/60 focus:ring-1 focus:ring-amber-500/20"
        />
        {suffix && (
          <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[10px] text-stone-400">
            {suffix}
          </span>
        )}
      </span>
    </label>
  )
}

function VectorField({
  label,
  value,
  step = 0.01,
  onCommit,
}: {
  label: string
  value: number[] | undefined
  step?: number
  onCommit: (value: number[]) => void
}) {
  const current = value ?? [0, 0, 0]
  return (
    <div className="flex flex-col gap-1 text-[11px] font-medium text-stone-500">
      {label}
      <div className="grid grid-cols-3 gap-1">
        {[0, 1, 2].map((index) => (
          <input
            key={index}
            type="number"
            step={step}
            value={current[index] ?? 0}
            onChange={(event) => {
              const next = [...current]
              next[index] = Number(event.target.value)
              onCommit(next)
            }}
            className="w-full rounded-lg border border-border-subtle bg-base px-2 py-1.5 text-xs text-stone-800 outline-none focus:border-amber-500/60"
          />
        ))}
      </div>
    </div>
  )
}

function ValidationPanel({ validation }: { validation: FoundryValidation | null }) {
  if (!validation) {
    return (
      <p className="rounded-xl border border-dashed border-border-subtle bg-base/50 px-3 py-4 text-[11px] leading-relaxed text-stone-400">
        No validation yet. Generate the MJCF to run the static checks and the real MuJoCo
        compile.
      </p>
    )
  }
  const icon =
    validation.status === 'ok' ? (
      <CheckCircle2 size={15} className="text-emerald-600" />
    ) : validation.status === 'warning' ? (
      <TriangleAlert size={15} className="text-amber-600" />
    ) : (
      <TriangleAlert size={15} className="text-red-600" />
    )
  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-center gap-2">
        {icon}
        <span className="text-xs font-semibold capitalize text-stone-700">
          {validation.status === 'ok' ? 'Compiles cleanly' : validation.status}
        </span>
      </div>
      <ul className="grid gap-1">
        {Object.entries(validation.checks ?? {}).map(([name, passed]) => (
          <li key={name} className="flex items-center justify-between gap-2 text-[11px]">
            <span className="text-stone-500">{name.replaceAll('_', ' ')}</span>
            <span className={passed ? 'font-semibold text-emerald-600' : 'font-semibold text-red-600'}>
              {passed ? 'pass' : 'fail'}
            </span>
          </li>
        ))}
      </ul>
      {(validation.warnings ?? []).map((warning) => (
        <p key={warning} className="rounded-lg bg-amber-50 px-2.5 py-1.5 text-[11px] leading-relaxed text-amber-700">
          {warning}
        </p>
      ))}
      {(validation.errors ?? []).map((error) => (
        <p key={error} className="rounded-lg bg-red-50 px-2.5 py-1.5 text-[11px] leading-relaxed text-red-700">
          {error}
        </p>
      ))}
      {validation.note && (
        <p className="text-[10px] leading-relaxed text-stone-400">{validation.note}</p>
      )}
    </div>
  )
}

function ComponentProperties({
  component,
  joint,
  actuator,
}: {
  component: FoundryComponent
  joint: FoundryJoint | undefined
  actuator: FoundryActuator | undefined
}) {
  const updateComponent = useFoundryStore((s) => s.updateComponent)
  const deleteComponent = useFoundryStore((s) => s.deleteComponent)
  const updateJoint = useFoundryStore((s) => s.updateJoint)
  const updateActuator = useFoundryStore((s) => s.updateActuator)
  const saving = useFoundryStore((s) => s.saving)
  const allComponents = useFoundryStore((s) => s.detail?.components ?? [])
  const [confirmDelete, setConfirmDelete] = useState(false)

  const physics = component.metadata.physics ?? {}
  const hidden = component.metadata.visible === false
  const dimensions = component.metadata.dimensions ?? [0.1, 0.1, 0.1]
  const method =
    component.metadata.reconstruction_method ??
    (component.metadata.mesh?.method as string | undefined) ??
    'unknown'

  return (
    <div className="flex flex-col gap-4">
      <section className="flex flex-col gap-2.5">
        <div className="flex items-center justify-between">
          <h4 className="text-[11px] font-bold uppercase tracking-wide text-stone-400">Part</h4>
          <span className="text-[10px] text-stone-400">{saving ? 'saving…' : `confidence ${percent(component.confidence)}`}</span>
        </div>
        <label className="flex flex-col gap-1 text-[11px] font-medium text-stone-500">
          Label
          <input
            defaultValue={component.label}
            key={`label-${component.id}`}
            onBlur={(event) => {
              const label = event.target.value.trim()
              if (label && label !== component.label) void updateComponent(component.id, { label })
            }}
            className="rounded-lg border border-border-subtle bg-base px-2.5 py-1.5 text-xs text-stone-800 outline-none focus:border-amber-500/60"
          />
        </label>
        <label className="flex flex-col gap-1 text-[11px] font-medium text-stone-500">
          Type
          <select
            value={component.component_type}
            onChange={(event) => void updateComponent(component.id, { component_type: event.target.value })}
            className="rounded-lg border border-border-subtle bg-base px-2.5 py-1.5 text-xs text-stone-800 outline-none focus:border-amber-500/60"
          >
            {COMPONENT_TYPES.map((type) => (
              <option key={type} value={type}>{type}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-[11px] font-medium text-stone-500">
          Parent part
          <select
            value={component.parent_id ?? ''}
            onChange={(event) => {
              if (event.target.value) void updateComponent(component.id, { parent_id: event.target.value })
            }}
            className="rounded-lg border border-border-subtle bg-base px-2.5 py-1.5 text-xs text-stone-800 outline-none focus:border-amber-500/60"
          >
            {component.parent_id === null && <option value="">Root body</option>}
            {allComponents
              .filter((candidate) => candidate.id !== component.id)
              .map((candidate) => (
                <option key={candidate.id} value={candidate.id}>{candidate.label}</option>
              ))}
          </select>
        </label>
        <VectorField
          label="Dimensions (x, y, z) m"
          value={dimensions}
          onCommit={(next) => void updateComponent(component.id, { dimensions: next.map((value) => Math.max(0.001, value)) })}
        />
        <div className="grid grid-cols-2 gap-2">
          <NumberField
            label="Mass"
            value={physics.mass_kg}
            step={0.01}
            min={0.001}
            suffix="kg"
            onCommit={(value) => void updateComponent(component.id, { mass_kg: Math.max(0.001, value) })}
          />
          <NumberField
            label="Friction"
            value={physics.friction}
            step={0.05}
            min={0}
            onCommit={(value) => void updateComponent(component.id, { friction: Math.max(0, value) })}
          />
        </div>
        {physics.estimated !== false ? (
          <p className="rounded-lg bg-amber-50 px-2.5 py-1.5 text-[10px] leading-relaxed text-amber-700">
            Estimated from visible geometry and an assumed density. Edit a value to mark it as
            user-confirmed.
          </p>
        ) : (
          <p className="rounded-lg bg-emerald-50 px-2.5 py-1.5 text-[10px] text-emerald-700">
            User-confirmed physics values.
          </p>
        )}
        <p className="font-mono text-[10px] text-stone-400">mesh: {method}</p>
        <div className="flex items-center gap-2 pt-1">
          <button
            type="button"
            onClick={() => void updateComponent(component.id, { visible: hidden })}
            className="rounded-lg border border-border-subtle bg-white px-2.5 py-1 text-[11px] font-medium text-stone-600 transition-colors hover:bg-stone-50"
          >
            {hidden ? 'Show part' : 'Hide part'}
          </button>
          <button
            type="button"
            disabled={saving}
            onClick={() => {
              if (confirmDelete) {
                void deleteComponent(component.id)
                setConfirmDelete(false)
              } else {
                setConfirmDelete(true)
              }
            }}
            className={clsx(
              'ml-auto rounded-lg px-2.5 py-1 text-[11px] font-medium transition-colors disabled:opacity-50',
              confirmDelete
                ? 'bg-red-100 text-red-700'
                : 'text-stone-400 hover:bg-red-50 hover:text-red-600',
            )}
          >
            {confirmDelete ? 'Confirm delete' : 'Delete part'}
          </button>
        </div>
      </section>

      {joint && (
        <section className="flex flex-col gap-2.5 border-t border-border-subtle pt-3">
          <h4 className="text-[11px] font-bold uppercase tracking-wide text-stone-400">
            Joint {joint.joint_type === 'fixed' ? '(fixed)' : '(inferred)'}
          </h4>
          <label className="flex flex-col gap-1 text-[11px] font-medium text-stone-500">
            Type
            <select
              value={joint.joint_type}
              onChange={(event) => void updateJoint(joint.id, { joint_type: event.target.value })}
              className="rounded-lg border border-border-subtle bg-base px-2.5 py-1.5 text-xs text-stone-800 outline-none focus:border-amber-500/60"
            >
              {JOINT_TYPES.map((type) => (
                <option key={type} value={type}>{type}</option>
              ))}
            </select>
          </label>
          <VectorField
            label="Axis"
            value={joint.axis}
            step={0.1}
            onCommit={(next) => void updateJoint(joint.id, { axis: next })}
          />
          <VectorField
            label="Anchor position"
            value={joint.position}
            step={0.01}
            onCommit={(next) => void updateJoint(joint.id, { position: next })}
          />
          <div className="grid grid-cols-2 gap-2">
            <NumberField
              label="Lower limit"
              value={joint.limits?.[0]}
              step={0.05}
              onCommit={(value) => {
                const upper = joint.limits?.[1] ?? 1
                if (value < upper) void updateJoint(joint.id, { limits: [value, upper] })
              }}
            />
            <NumberField
              label="Upper limit"
              value={joint.limits?.[1]}
              step={0.05}
              onCommit={(value) => {
                const lower = joint.limits?.[0] ?? -1
                if (value > lower) void updateJoint(joint.id, { limits: [lower, value] })
              }}
            />
          </div>
        </section>
      )}

      {actuator && (
        <section className="flex flex-col gap-2.5 border-t border-border-subtle pt-3">
          <h4 className="text-[11px] font-bold uppercase tracking-wide text-stone-400">Actuator</h4>
          <p className="text-[11px] text-stone-500">
            Type: <span className="font-mono">{actuator.actuator_type}</span>
            <span className="text-stone-400"> (position control, required by the joint tests)</span>
          </p>
          <div className="grid grid-cols-2 gap-2">
            <NumberField
              label="kp"
              value={actuator.configuration.kp}
              step={1}
              min={0}
              onCommit={(value) => void updateActuator(actuator.id, { kp: Math.max(0, value) })}
            />
            <NumberField
              label="kv"
              value={actuator.configuration.kv}
              step={0.1}
              min={0}
              onCommit={(value) => void updateActuator(actuator.id, { kv: Math.max(0, value) })}
            />
          </div>
          <NumberField
            label="Gear ratio"
            value={actuator.configuration.gear}
            step={0.1}
            min={0.01}
            onCommit={(value) => void updateActuator(actuator.id, { gear: Math.max(0.01, value) })}
          />
          <div className="grid grid-cols-2 gap-2">
            <NumberField
              label="Control min"
              value={actuator.configuration.ctrlrange?.[0]}
              step={0.1}
              onCommit={(value) => {
                const upper = actuator.configuration.ctrlrange?.[1] ?? 1
                if (value < upper) void updateActuator(actuator.id, { ctrlrange: [value, upper] })
              }}
            />
            <NumberField
              label="Control max"
              value={actuator.configuration.ctrlrange?.[1]}
              step={0.1}
              onCommit={(value) => {
                const lower = actuator.configuration.ctrlrange?.[0] ?? -1
                if (value > lower) void updateActuator(actuator.id, { ctrlrange: [lower, value] })
              }}
            />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <NumberField
              label="Force min"
              value={actuator.configuration.forcerange?.[0]}
              step={1}
              onCommit={(value) => {
                const upper = actuator.configuration.forcerange?.[1] ?? 100
                if (value < upper) void updateActuator(actuator.id, { forcerange: [value, upper] })
              }}
            />
            <NumberField
              label="Force max"
              value={actuator.configuration.forcerange?.[1]}
              step={1}
              onCommit={(value) => {
                const lower = actuator.configuration.forcerange?.[0] ?? -100
                if (value > lower) void updateActuator(actuator.id, { forcerange: [lower, value] })
              }}
            />
          </div>
          <p className="text-[10px] leading-relaxed text-stone-400">
            Gains and ranges flow into the generated MJCF on the next Generate run.
          </p>
        </section>
      )}
    </div>
  )
}

function AddPartForm() {
  const addComponent = useFoundryStore((s) => s.addComponent)
  const saving = useFoundryStore((s) => s.saving)
  const [expanded, setExpanded] = useState(false)
  const [label, setLabel] = useState('')

  if (!expanded) {
    return (
      <button
        type="button"
        onClick={() => setExpanded(true)}
        className="mb-2 rounded-lg border border-dashed border-border-subtle bg-base/50 px-2 py-1.5 text-[11px] font-medium text-stone-500 transition-colors hover:border-amber-300 hover:text-amber-700"
      >
        + Add a missing part
      </button>
    )
  }

  const submit = () => {
    if (!label.trim()) return
    void addComponent(label.trim()).then(() => {
      setLabel('')
      setExpanded(false)
    })
  }

  return (
    <div className="mb-2 flex gap-1.5">
      <input
        value={label}
        onChange={(event) => setLabel(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') submit()
          if (event.key === 'Escape') setExpanded(false)
        }}
        placeholder="Part label, e.g. Antenna"
        maxLength={80}
        autoFocus
        disabled={saving}
        className="min-w-0 flex-1 rounded-lg border border-border-subtle bg-base px-2 py-1.5 text-xs text-stone-800 outline-none focus:border-amber-500/60 disabled:opacity-50"
      />
      <button
        type="button"
        onClick={submit}
        disabled={!label.trim() || saving}
        className="rounded-lg bg-stone-900 px-2.5 py-1.5 text-[11px] font-semibold text-white transition-colors hover:bg-stone-700 disabled:cursor-not-allowed disabled:opacity-45"
      >
        Add
      </button>
    </div>
  )
}

export function FoundryWorkspace() {
  const activeId = useFoundryStore((s) => s.activeId)
  const detail = useFoundryStore((s) => s.detail)
  const detailStatus = useFoundryStore((s) => s.detailStatus)
  const detailError = useFoundryStore((s) => s.detailError)
  const job = useFoundryStore((s) => s.job)
  const activeStage = useFoundryStore((s) => s.activeStage)
  const actionError = useFoundryStore((s) => s.actionError)
  const uploading = useFoundryStore((s) => s.uploading)
  const simulating = useFoundryStore((s) => s.simulating)
  const simulateResult = useFoundryStore((s) => s.simulateResult)
  const selectProject = useFoundryStore((s) => s.selectProject)
  const refreshDetail = useFoundryStore((s) => s.refreshDetail)
  const uploadImages = useFoundryStore((s) => s.uploadImages)
  const runStage = useFoundryStore((s) => s.runStage)
  const simulate = useFoundryStore((s) => s.simulate)
  const clearActionError = useFoundryStore((s) => s.clearActionError)
  const setCenterTab = useWorkspaceStore((s) => s.setCenterTab)

  const fileInput = useRef<HTMLInputElement>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const components = detail?.components ?? []
  const selected = components.find((component) => component.id === selectedId) ?? null
  const selectedJoint = detail?.joints.find((joint) => joint.component_id === selectedId)
  const selectedActuator = selectedJoint
    ? detail?.actuators.find((actuator) => actuator.joint_id === selectedJoint.id)
    : undefined

  useEffect(() => {
    if (selectedId && components.some((component) => component.id === selectedId)) return
    setSelectedId(components[0]?.id ?? null)
  }, [components, selectedId])

  const latestVersion = detail?.versions[0] ?? null
  const validation = latestVersion?.validation ?? null
  const hasImages = (detail?.images.length ?? 0) > 0
  const busy = activeStage !== null
  const canSimulate = components.length > 0

  const stageState = useMemo(() => {
    const done = new Set<string>()
    if (detail?.analysis) done.add('analyze')
    if (components.some((component) => component.mesh_url)) done.add('reconstruct')
    if (detail?.joints.length) done.add('assemble')
    if (latestVersion) done.add('generate-mjcf')
    return done
  }, [detail, components, latestVersion])

  const orderedComponents = useMemo(() => hierarchyRows(components), [components])

  const handleUpload = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    await uploadImages(Array.from(files), 'reference')
    if (fileInput.current) fileInput.current.value = ''
  }

  const handleSimulate = async () => {
    const result = await simulate()
    if (result) {
      setCenterTab('simulator')
      useFoundryStore.getState().setOpen(false)
    }
  }

  if (!activeId) return null

  if (detailStatus === 'loading' && !detail) {
    return (
      <div className="flex flex-1 items-center justify-center p-8">
        <LoadingRow label="Loading Foundry project…" />
      </div>
    )
  }

  if (detailStatus === 'error' && !detail) {
    return (
      <div className="flex flex-1 items-center justify-center p-8">
        <ErrorNotice message={detailError ?? 'Could not load the Foundry project.'} onRetry={() => void refreshDetail()} />
      </div>
    )
  }

  if (!detail) return null

  const project = detail.project
  const analysis = detail.analysis

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border-subtle bg-sidebar/80 px-4 py-2.5">
        <button
          type="button"
          onClick={() => void selectProject(null)}
          className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs font-medium text-stone-500 transition-colors hover:bg-stone-100 hover:text-stone-800"
        >
          <ArrowLeft size={14} /> All robots
        </button>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="truncate text-sm font-bold text-stone-800">{project.name}</h2>
            <span className="rounded-full bg-stone-100 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-stone-600">
              {project.status.replaceAll('_', ' ')}
            </span>
          </div>
          <p className="text-[11px] capitalize text-stone-400">
            {project.category} · {components.length} parts · {detail.joints.length} joints · v
            {latestVersion?.version ?? '—'}
          </p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void refreshDetail()}
            title="Refresh the project from the server"
            className="inline-flex items-center gap-1.5 rounded-lg border border-border-subtle bg-white px-2.5 py-1.5 text-xs font-medium text-stone-600 shadow-sm transition-colors hover:bg-stone-50"
          >
            <RefreshCw size={13} /> Refresh
          </button>
          <a
            href={foundryExportUrl(project.id)}
            download
            className="inline-flex items-center gap-1.5 rounded-lg border border-border-subtle bg-white px-2.5 py-1.5 text-xs font-medium text-stone-600 shadow-sm transition-colors hover:bg-stone-50"
          >
            <Download size={13} /> Export package
          </a>
          <ActionButton
            onClick={() => void handleSimulate()}
            disabled={!canSimulate || simulating || busy}
            variant="primary"
            title="Copy the generated robot into its linked sequence and open the simulator"
          >
            {simulating ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            {simulating ? 'Opening…' : 'Open in Simulator'}
          </ActionButton>
        </div>
      </div>

      {actionError && (
        <div className="shrink-0 px-4 pt-2.5">
          <ErrorNotice message={actionError} onRetry={() => clearActionError()} />
        </div>
      )}
      {simulateResult && (
        <p className="shrink-0 px-4 pt-2.5 text-xs text-emerald-700">
          Robot copied to the sequence ({simulateResult.binary_files.length} binary assets). Open
          the simulator to run the joint tests.
        </p>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 p-3 xl:grid-cols-[280px_minmax(0,1fr)_340px]">
        <aside className="flex min-h-0 flex-col gap-3 overflow-y-auto">
          <section className="rounded-xl border border-border-subtle bg-elevated p-3 shadow-sm">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-xs font-bold text-stone-800">Reference images</h3>
              <button
                type="button"
                onClick={() => fileInput.current?.click()}
                disabled={uploading}
                className="inline-flex items-center gap-1 rounded-lg border border-border-subtle bg-white px-2 py-1 text-[11px] font-medium text-stone-600 hover:bg-stone-50 disabled:opacity-50"
              >
                <ImagePlus size={12} /> {uploading ? 'Uploading…' : 'Add'}
              </button>
              <input
                ref={fileInput}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                multiple
                className="hidden"
                onChange={(event) => void handleUpload(event.target.files)}
              />
            </div>
            {detail.images.length === 0 ? (
              <button
                type="button"
                onClick={() => fileInput.current?.click()}
                className="flex w-full flex-col items-center gap-1.5 rounded-lg border border-dashed border-border-subtle bg-base/50 px-3 py-6 text-center text-[11px] text-stone-400 hover:border-amber-300 hover:text-amber-700"
              >
                <ImagePlus size={20} />
                Upload a PNG, JPG, or WEBP photo
              </button>
            ) : (
              <ul className="grid grid-cols-2 gap-2">
                {detail.images.map((image) => (
                  <li key={image.id} className="overflow-hidden rounded-lg border border-border-subtle bg-base/50">
                    <img src={image.asset_url} alt={image.filename} className="h-20 w-full object-cover" />
                    <p className="truncate px-1.5 py-1 text-[10px] text-stone-400">{image.filename}</p>
                  </li>
                ))}
              </ul>
            )}
            {analysis && (
              <p className="mt-2 rounded-lg bg-stone-50 px-2 py-1.5 text-[10px] leading-relaxed text-stone-500">
                Segmentation: <span className="font-mono">{analysis.method ?? 'unknown'}</span>
                {analysis.provider_error ? ' (provider failed; heuristic fallback)' : ''}
              </p>
            )}
          </section>

          <section className="flex min-h-0 flex-col rounded-xl border border-border-subtle bg-elevated p-3 shadow-sm">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-xs font-bold text-stone-800">Parts</h3>
              <span className="text-[10px] text-stone-400">{components.length}</span>
            </div>
            <AddPartForm />
            {components.length === 0 ? (
              <p className="px-1 py-4 text-[11px] leading-relaxed text-stone-400">
                Run Analyze to segment the reference image into reviewable parts.
              </p>
            ) : (
              <ul className="flex flex-col gap-1">
                {orderedComponents.map(({ component, depth }) => {
                  const active = component.id === selectedId
                  return (
                    <li key={component.id}>
                      <button
                        type="button"
                        onClick={() => setSelectedId(component.id)}
                        style={{ paddingLeft: 8 + Math.min(depth, 4) * 14 }}
                        className={clsx(
                          'flex w-full items-center gap-2 rounded-lg py-1.5 pr-2 text-left text-xs transition-colors',
                          active ? 'bg-amber-100 font-medium text-amber-900' : 'text-stone-600 hover:bg-stone-100',
                        )}
                      >
                        {depth > 0 && <span className="text-stone-300">└</span>}
                        <Box size={13} className={active ? 'text-amber-600' : 'text-stone-400'} />
                        <span className="min-w-0 flex-1 truncate">{component.label}</span>
                        <span className="rounded bg-white/60 px-1 py-0.5 text-[9px] text-stone-400">
                          {percent(component.confidence)}
                        </span>
                        {component.metadata.physics?.estimated !== false && (
                          <span className="rounded bg-amber-200/70 px-1 py-0.5 text-[9px] font-semibold text-amber-800">
                            est
                          </span>
                        )}
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        </aside>

        <section className="flex min-h-[420px] min-w-0 flex-col overflow-hidden rounded-xl border border-border-subtle bg-elevated shadow-sm">
          <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-border-subtle/80 bg-sidebar/50 px-3 py-2">
            {FOUNDRY_STAGES.map((stage) => (
              <button
                key={stage.id}
                type="button"
                onClick={() => void runStage(stage.id)}
                disabled={!hasImages || busy}
                title={stage.hint}
                className={clsx(
                  'inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-45',
                  stageState.has(stage.id)
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                    : 'border-border-subtle bg-white text-stone-600 hover:bg-stone-50',
                )}
              >
                {activeStage === stage.id ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : stageState.has(stage.id) ? (
                  <CheckCircle2 size={12} />
                ) : (
                  <Sparkles size={12} />
                )}
                {stage.label}
              </button>
            ))}
            <button
              type="button"
              onClick={() => void runStage('generate')}
              disabled={!hasImages || busy}
              className="ml-auto inline-flex items-center gap-1.5 rounded-lg bg-stone-900 px-2.5 py-1.5 text-[11px] font-semibold text-white transition-colors hover:bg-stone-700 disabled:cursor-not-allowed disabled:opacity-45"
              title="Run analyze → reconstruct → assemble → generate in one job"
            >
              {activeStage === 'generate' ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
              Generate all
            </button>
          </div>

          <div className="relative min-h-0 flex-1">
            {components.length > 0 ? (
              <FoundryViewport
                components={components}
                joints={detail.joints}
                selectedId={selectedId}
                onSelect={setSelectedId}
                className="h-full w-full"
              />
            ) : (
              <div className="flex h-full items-center justify-center p-6 text-center">
                <div className="max-w-sm">
                  <Box size={34} className="mx-auto text-stone-300" />
                  <p className="mt-2 text-sm font-semibold text-stone-600">Nothing to show yet</p>
                  <p className="mt-1 text-xs leading-relaxed text-stone-400">
                    Upload a reference image and run Analyze. Reconstructed meshes appear here as
                    soon as the 3D stage finishes.
                  </p>
                </div>
              </div>
            )}
            {busy && (
              <div className="absolute inset-x-0 bottom-0 border-t border-border-subtle bg-white/90 px-3 py-2 backdrop-blur">
                <div className="flex items-center justify-between text-[11px] text-stone-500">
                  <span>{job?.message ?? 'Starting…'}</span>
                  <span className="font-mono">{job?.progress ?? 0}%</span>
                </div>
                <div className="mt-1 h-1 overflow-hidden rounded-full bg-stone-200">
                  <div
                    className="h-full rounded-full bg-amber-500 transition-all"
                    style={{ width: `${job?.progress ?? 0}%` }}
                  />
                </div>
              </div>
            )}
          </div>
        </section>

        <aside className="flex min-h-0 flex-col gap-3 overflow-y-auto">
          <section className="rounded-xl border border-border-subtle bg-elevated p-3 shadow-sm">
            <div className="mb-2 flex items-center gap-1.5">
              <ShieldCheck size={14} className="text-stone-400" />
              <h3 className="text-xs font-bold text-stone-800">Validation</h3>
            </div>
            <ValidationPanel validation={validation} />
          </section>

          <section className="rounded-xl border border-border-subtle bg-elevated p-3 shadow-sm">
            <h3 className="mb-2 text-xs font-bold text-stone-800">Properties</h3>
            {selected ? (
              <ComponentProperties
                key={selected.id}
                component={selected}
                joint={selectedJoint}
                actuator={selectedActuator}
              />
            ) : (
              <p className="text-[11px] leading-relaxed text-stone-400">
                Select a part to review its geometry, estimated physics, joint, and actuator.
              </p>
            )}
          </section>
        </aside>
      </div>
    </div>
  )
}
