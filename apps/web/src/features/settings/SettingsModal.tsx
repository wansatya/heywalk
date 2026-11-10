import { useEffect, useState } from 'react'
import { CheckCircle2, Settings as SettingsIcon, TriangleAlert, X } from 'lucide-react'
import { useSettingsStore } from '../../stores/settings'
import type { ModelSettings } from '../../api/types'
import { ActionButton, ErrorNotice, LoadingRow } from '../../components/Panel'
import { clsx } from '../../components/clsx'

const INPUT_CLASS =
  'rounded-lg border border-border-subtle bg-base px-3 py-2 text-sm text-stone-800 outline-none transition-all placeholder:text-stone-400 focus:border-amber-500/60 focus:ring-1 focus:ring-amber-500/20'

function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <label className="flex flex-col gap-1 text-xs font-medium text-stone-500">
      {label}
      {children}
      {hint && <span className="text-[11px] font-normal text-stone-400">{hint}</span>}
    </label>
  )
}

/**
 * Model settings form. Remounted by the parent whenever the saved settings
 * change, so drafts always start from the latest server state.
 */
function SettingsForm({ initial }: { initial: ModelSettings }) {
  const save = useSettingsStore((s) => s.save)
  const saving = useSettingsStore((s) => s.saving)
  const saveError = useSettingsStore((s) => s.saveError)
  const savedAt = useSettingsStore((s) => s.savedAt)

  const [baseUrl, setBaseUrl] = useState(initial.base_url)
  const [model, setModel] = useState(initial.model)
  const [apiKey, setApiKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [dirtySinceSave, setDirtySinceSave] = useState(false)

  const changed =
    baseUrl.trim() !== initial.base_url ||
    model.trim() !== initial.model ||
    apiKey.trim() !== ''

  const markDirty = () => setDirtySinceSave(true)

  const handleSave = async () => {
    const patch: { base_url?: string; model?: string; api_key?: string } = {}
    if (baseUrl.trim() !== initial.base_url) patch.base_url = baseUrl.trim()
    if (model.trim() !== initial.model) patch.model = model.trim()
    if (apiKey.trim() !== '') patch.api_key = apiKey.trim()
    if (Object.keys(patch).length === 0) return
    const ok = await save(patch)
    if (ok) {
      setApiKey('')
      setDirtySinceSave(false)
    }
  }

  const handleRemoveKey = async () => {
    const ok = await save({ api_key: '' })
    if (ok) {
      setApiKey('')
      setDirtySinceSave(false)
    }
  }

  const envPinned: string[] = []
  if (initial.api_key_from_env) envPinned.push('API key')
  if (initial.base_url_from_env) envPinned.push('API URL')
  if (initial.model_from_env) envPinned.push('model')

  return (
    <div className="space-y-3.5">
      <p className="text-xs leading-relaxed text-stone-500">
        Any OpenAI-compatible Chat Completions endpoint works. Changes apply to the
        next generate/fix call and are saved to the server{' '}
        <span className="font-mono text-stone-600">.env</span> so a restart keeps them.
      </p>

      <div
        className={clsx(
          'flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-medium',
          initial.configured
            ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
            : 'border-stone-200 bg-stone-50 text-stone-500',
        )}
      >
        <span
          className={clsx(
            'h-2 w-2 rounded-full',
            initial.configured ? 'bg-emerald-500' : 'bg-stone-400',
          )}
        />
        {initial.configured
          ? `API key set (${initial.key_hint || 'hidden'}) — AI generation enabled`
          : 'No API key — AI generation is disabled until you add one'}
      </div>

      <Field label="API base URL" hint="Must start with http(s):// — /chat/completions is appended.">
        <input
          value={baseUrl}
          onChange={(e) => {
            setBaseUrl(e.target.value)
            markDirty()
          }}
          placeholder="https://api.example.com/v1"
          maxLength={500}
          spellCheck={false}
          className={clsx(INPUT_CLASS, 'w-full font-mono text-[13px]')}
        />
      </Field>

      <Field label="Model name" hint="Sent as the chat request model.">
        <input
          value={model}
          onChange={(e) => {
            setModel(e.target.value)
            markDirty()
          }}
          placeholder="model-name"
          maxLength={200}
          spellCheck={false}
          className={clsx(INPUT_CLASS, 'w-full font-mono text-[13px]')}
        />
      </Field>

      <Field
        label="API key"
        hint={
          initial.configured
            ? 'Leave blank to keep the current key.'
            : 'Stored server-side only — never sent back to the browser.'
        }
      >
        <span className="relative block">
          <input
            type={showKey ? 'text' : 'password'}
            value={apiKey}
            onChange={(e) => {
              setApiKey(e.target.value)
              markDirty()
            }}
            placeholder={initial.configured ? '•••••••• (unchanged)' : 'sk-…'}
            autoComplete="off"
            spellCheck={false}
            className={clsx(INPUT_CLASS, 'w-full pr-16 font-mono text-[13px]')}
          />
          <button
            type="button"
            onClick={() => setShowKey((v) => !v)}
            className="absolute right-2 top-1/2 -translate-y-1/2 rounded px-1.5 py-0.5 text-[11px] font-medium text-stone-500 hover:bg-stone-200/60 hover:text-stone-800"
          >
            {showKey ? 'Hide' : 'Show'}
          </button>
        </span>
      </Field>

      {envPinned.length > 0 && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-800">
          <TriangleAlert size={14} className="mt-0.5 shrink-0" />
          <span>
            {envPinned.join(' · ')} {envPinned.length === 1 ? 'is' : 'are'} also set as a
            server environment variable — changes here apply now, but a restart restores
            the env value.
          </span>
        </div>
      )}

      {!initial.persisted && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-800">
          <TriangleAlert size={14} className="mt-0.5 shrink-0" />
          <span>
            Saved for this session only — writing to the server .env failed. The values
            above are still active until restart.
          </span>
        </div>
      )}

      {saveError && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-700">
          Save failed: {saveError}
        </div>
      )}

      {savedAt !== null && !dirtySinceSave && !saveError && (
        <div className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs font-medium text-emerald-700">
          <CheckCircle2 size={14} />
          Saved — takes effect immediately.
        </div>
      )}

      <div className="flex items-center justify-between gap-2.5 pt-1">
        {initial.configured ? (
          <ActionButton
            onClick={() => void handleRemoveKey()}
            disabled={saving}
            variant="danger"
            title="Remove the stored API key (disables AI generation)"
          >
            Remove key
          </ActionButton>
        ) : (
          <span />
        )}
        <ActionButton
          onClick={() => void handleSave()}
          disabled={saving || !changed}
          variant="primary"
          title={changed ? 'Save settings' : 'No changes'}
        >
          {saving ? 'Saving…' : 'Save settings'}
        </ActionButton>
      </div>

      {initial.persist_path && (
        <p className="font-mono text-[11px] text-stone-400">
          persisted to {initial.persist_path}
        </p>
      )}
    </div>
  )
}

/**
 * Settings modal: view and update the AI-model endpoint, model name, and API
 * key without restarting the server.
 */
export function SettingsModal() {
  const open = useSettingsStore((s) => s.open)
  const setOpen = useSettingsStore((s) => s.setOpen)
  const settings = useSettingsStore((s) => s.settings)
  const refresh = useSettingsStore((s) => s.refresh)
  const saving = useSettingsStore((s) => s.saving)

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, setOpen])

  if (!open) return null

  const data = settings.data
  // Remount the form on every successful save so drafts track server state.
  const formKey = data ? `${data.base_url}|${data.model}|${data.key_hint}` : 'empty'

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-stone-950/45 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="AI model settings"
      onClick={() => {
        if (!saving) setOpen(false)
      }}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-border-subtle bg-elevated shadow-xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex shrink-0 items-center justify-between gap-2.5 border-b border-border-subtle/80 bg-sidebar/70 px-4 py-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-stone-900">
            <SettingsIcon size={15} className="text-stone-500" />
            AI model settings
          </h2>
          <button
            type="button"
            onClick={() => setOpen(false)}
            disabled={saving}
            title="Close"
            className="rounded-lg p-1.5 text-stone-500 transition-colors hover:bg-stone-200/60 hover:text-stone-900 disabled:opacity-40"
          >
            <X size={15} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {settings.status === 'loading' && !data && (
            <LoadingRow label="Loading model settings…" />
          )}
          {settings.status === 'error' && !data && settings.error && (
            <ErrorNotice message={settings.error} onRetry={() => void refresh()} />
          )}
          {data && <SettingsForm key={formKey} initial={data} />}
        </div>
      </div>
    </div>
  )
}
