import { Component, Suspense, lazy, useEffect, useRef, useState, type ReactNode } from 'react'
import { FileDown, GitCompare, Save, Sparkles } from 'lucide-react'
import { CONTROLLER_PATH, useWorkspaceStore } from '../../stores/workspace'
import { useExperimentStore } from '../../stores/experiment'
import { ActionButton, ErrorNotice, LoadingRow, Panel } from '../../components/Panel'

const MonacoEditor = lazy(() => import('@monaco-editor/react'))
const MonacoDiffEditor = lazy(() =>
  import('@monaco-editor/react').then((m) => ({ default: m.DiffEditor })),
)

class EditorErrorBoundary extends Component<{ children: ReactNode; fallback: ReactNode }, { failed: boolean }> {
  constructor(props: { children: ReactNode; fallback: ReactNode }) {
    super(props)
    this.state = { failed: false }
  }

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true }
  }

  render(): ReactNode {
    return this.state.failed ? this.props.fallback : this.props.children
  }
}

function PlainTextFallback({
  value,
  onChange,
}: {
  value: string
  onChange: (value: string) => void
}) {
  return (
    <div className="flex h-full flex-col">
      <p className="shrink-0 border-b border-amber-200 bg-amber-50 px-3.5 py-2 text-xs font-medium text-amber-900">
        Code editor failed to load (offline?). Plain-text editing is available instead.
      </p>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        spellCheck={false}
        className="min-h-0 flex-1 resize-none bg-base p-3.5 font-mono text-xs leading-relaxed text-stone-800 outline-none"
      />
    </div>
  )
}

export function ControllerEditor() {
  const files = useWorkspaceStore((s) => s.files)
  const activeFilePath = useWorkspaceStore((s) => s.activeFilePath)
  const editorContent = useWorkspaceStore((s) => s.editorContent)
  const editorDirty = useWorkspaceStore((s) => s.editorDirty)
  const editorVersion = useWorkspaceStore((s) => s.editorVersion)
  const filesState = useWorkspaceStore((s) => s.filesState)
  const filesError = useWorkspaceStore((s) => s.filesError)
  const editContent = useWorkspaceStore((s) => s.editContent)
  const saveActiveFile = useWorkspaceStore((s) => s.saveActiveFile)
  const saveState = useWorkspaceStore((s) => s.saveState)
  const generate = useWorkspaceStore((s) => s.generate)
  const generation = useWorkspaceStore((s) => s.generation)
  const generationHint = useWorkspaceStore((s) => s.generationHint)
  const fixFromRun = useWorkspaceStore((s) => s.fixFromRun)
  const fix = useWorkspaceStore((s) => s.fix)
  const fixHint = useWorkspaceStore((s) => s.fixHint)
  const diffBase = useWorkspaceStore((s) => s.diffBase)
  const importSample = useWorkspaceStore((s) => s.importSample)
  const sampleState = useWorkspaceStore((s) => s.sampleState)

  const activeRunId = useExperimentStore((s) => s.activeRunId)
  const runDetail = useExperimentStore((s) => s.runDetail)

  const [diffOn, setDiffOn] = useState(true)
  useEffect(() => {
    setDiffOn(true)
  }, [diffBase])
  const [confirmSample, setConfirmSample] = useState(false)
  useEffect(() => {
    setConfirmSample(false)
  }, [activeFilePath])

  const isController = activeFilePath === CONTROLLER_PATH
  const editorBlank = editorContent.trim() === ''
  const importing = sampleState.status === 'loading'
  const sampleArmed = confirmSample && !editorBlank
  const handleSample = () => {
    if (!editorBlank && !confirmSample) {
      setConfirmSample(true)
      return
    }
    setConfirmSample(false)
    void importSample()
  }
  const diffListener = useRef<{ dispose: () => void } | null>(null)
  useEffect(() => () => {
    diffListener.current?.dispose()
  }, [])
  const showDiff = diffOn && diffBase !== null && diffBase.path === activeFilePath

  const runFailed =
    runDetail !== null &&
    (runDetail.status === 'failed' || runDetail.status === 'timeout' || runDetail.status === 'error')

  return (
    <Panel
      title={activeFilePath ? `Code — ${activeFilePath}` : 'Code'}
      className="h-full"
      actions={
        <>
          {diffBase !== null && diffBase.path === activeFilePath && (
            <ActionButton
              onClick={() => setDiffOn(!diffOn)}
              variant={diffOn ? 'primary' : undefined}
              title={
                diffOn
                  ? 'Hide AI-change highlighting (green added, red removed)'
                  : 'Show AI-change highlighting (green added, red removed)'
              }
            >
              <GitCompare size={13} />
              Diff
            </ActionButton>
          )}
          {isController && (
            <ActionButton
              onClick={handleSample}
              disabled={importing}
              variant={sampleArmed ? 'danger' : undefined}
              title={
                sampleArmed
                  ? 'Click again to overwrite controller.py with the walk-to-flag sample script'
                  : 'Write the walk-to-flag sample script (resets grading to the flag)'
              }
            >
              <FileDown size={13} />
              {importing ? 'Importing…' : sampleArmed ? 'Confirm replace' : 'Sample'}
            </ActionButton>
          )}
          <ActionButton
            onClick={() => void saveActiveFile()}
            disabled={!activeFilePath || !editorDirty || saveState.status === 'loading'}
            title="Save the open file"
          >
            <Save size={13} />
            {saveState.status === 'loading' ? 'Saving…' : 'Save'}
          </ActionButton>
        </>
      }
    >
      <div className="flex h-full flex-col">
        {editorDirty && (
          <p className="shrink-0 border-b border-amber-200 bg-amber-50 px-3.5 py-1.5 text-xs font-medium text-amber-900">
            Unsaved changes — Start and Rerun save them first, so runs always use this version.
          </p>
        )}
        {isController && editorBlank && filesState !== 'loading' && (
          <p className="shrink-0 border-b border-sky-200 bg-sky-50 px-3.5 py-1.5 text-xs font-medium text-sky-900">
            controller.py is empty — import the walk-to-flag sample above, or describe a new
            objective in Task and press Generate.
          </p>
        )}

        {filesState === 'loading' && <LoadingRow label="Loading file…" />}
        {filesError && <ErrorNotice message={filesError} />}
        {generation.status === 'error' && generation.error && (
          <ErrorNotice
            message={`Generation failed: ${generation.error}`}
            hint={generationHint}
            onRetry={() => void generate()}
          />
        )}
        {fix.status === 'error' && fix.error && activeRunId && (
          <ErrorNotice
            message={`Diagnosis failed: ${fix.error}`}
            hint={fixHint}
            onRetry={() => void fixFromRun(activeRunId)}
          />
        )}
        {saveState.status === 'error' && saveState.error && (
          <ErrorNotice message={`Save failed: ${saveState.error}`} onRetry={() => void saveActiveFile()} />
        )}
        {sampleState.status === 'error' && sampleState.error && (
          <ErrorNotice message={`Sample import failed: ${sampleState.error}`} onRetry={() => void importSample()} />
        )}

        <div className="min-h-[220px] flex-1">
          {files.length === 0 && filesState !== 'loading' ? (
            <div className="flex h-full flex-col items-center justify-center gap-2.5 p-6 text-center text-xs text-stone-500">
              <Sparkles size={20} className="text-amber-600" />
              <p>
                No file open. Pick one in the explorer, or describe the task on the right and
                press Generate.
              </p>
            </div>
          ) : (
            <EditorErrorBoundary fallback={<PlainTextFallback value={editorContent} onChange={editContent} />}>
              <Suspense fallback={<LoadingRow label="Loading editor…" />}>
                {showDiff ? (
                  <MonacoDiffEditor
                    height="100%"
                    language="python"
                    theme="vs"
                    original={diffBase.content}
                    modified={editorContent}
                    onMount={(editor) => {
                      diffListener.current?.dispose()
                      const modifiedEditor = editor.getModifiedEditor()
                      diffListener.current = modifiedEditor.onDidChangeModelContent(() => {
                        editContent(modifiedEditor.getValue())
                      })
                    }}
                    options={{
                      renderSideBySide: false,
                      minimap: { enabled: false },
                      fontSize: 12,
                      lineHeight: 19,
                      fontFamily: "'JetBrains Mono', Consolas, monospace",
                      scrollBeyondLastLine: false,
                      automaticLayout: true,
                      renderWhitespace: 'trailing',
                      readOnly: false,
                    }}
                    loading={<LoadingRow label="Loading editor…" />}
                  />
                ) : (
                  <MonacoEditor
                    height="100%"
                    language="python"
                    theme="vs"
                    path={activeFilePath || 'controller.py'}
                    value={editorContent}
                    onChange={(value) => editContent(value ?? '')}
                    options={{
                      minimap: { enabled: false },
                      fontSize: 12,
                      lineHeight: 19,
                      fontFamily: "'JetBrains Mono', Consolas, monospace",
                      scrollBeyondLastLine: false,
                      automaticLayout: true,
                      tabSize: 4,
                      renderWhitespace: 'trailing',
                    }}
                    loading={<LoadingRow label="Loading editor…" />}
                  />
                )}
              </Suspense>
            </EditorErrorBoundary>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-t border-border-subtle/80 bg-sidebar/50 px-3.5 py-2 text-xs text-stone-500">
          <span>
            {activeFilePath ? (
              <>
                <span className="font-mono text-stone-800 font-medium">{activeFilePath}</span>
                {editorVersion !== null && ` · v${editorVersion}`}
                {editorDirty ? ' · unsaved' : saveState.status === 'success' ? ' · saved' : ''}
                {showDiff ? ' · green added, red removed by AI' : ''}
              </>
            ) : (
              'No file open'
            )}
          </span>
          {runFailed && <span className="text-red-700 font-medium">Last run failed — diagnostics are on the right.</span>}
        </div>
      </div>
    </Panel>
  )
}
