import { useState } from 'react'
import { ChevronDown, ChevronRight, FileCode2, Folder, FolderOpen, RefreshCw } from 'lucide-react'
import { DEFAULT_ROBOT_ID, useWorkspaceStore } from '../../stores/workspace'
import { ActionButton, ErrorNotice, LoadingRow, Panel } from '../../components/Panel'
import { clsx } from '../../components/clsx'

interface TreeNode {
  name: string
  path: string
  version?: number
  children: TreeNode[]
}

function buildTree(paths: { path: string; version: number }[]): TreeNode[] {
  const root: TreeNode[] = []
  for (const file of paths) {
    const parts = file.path.split('/')
    let level = root
    let prefix = ''
    for (let i = 0; i < parts.length; i++) {
      prefix = prefix ? `${prefix}/${parts[i]}` : parts[i]
      let node = level.find((n) => n.name === parts[i])
      if (!node) {
        node = { name: parts[i], path: prefix, children: [] }
        level.push(node)
      }
      if (i === parts.length - 1) node.version = file.version
      else level = node.children
    }
  }
  const sortLevel = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => {
      const aDir = a.children.length > 0 ? 0 : 1
      const bDir = b.children.length > 0 ? 0 : 1
      return aDir - bDir || a.name.localeCompare(b.name)
    })
    nodes.forEach((n) => sortLevel(n.children))
  }
  sortLevel(root)
  return root
}

function FileRow({ node, depth }: { node: TreeNode; depth: number }) {
  const activeFilePath = useWorkspaceStore((s) => s.activeFilePath)
  const editorDirty = useWorkspaceStore((s) => s.editorDirty)
  const openFile = useWorkspaceStore((s) => s.openFile)
  const active = node.path === activeFilePath
  return (
    <button
      type="button"
      onClick={() => void openFile(node.path)}
      title={`${node.path} · v${node.version ?? '?'}`}
      className={clsx(
        'group flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left font-mono text-xs transition-colors',
        active
          ? 'bg-amber-400/15 text-amber-200 font-medium shadow-xs'
          : 'text-slate-300 hover:bg-white/[0.05] hover:text-white',
      )}
      style={{ paddingLeft: `${depth * 14 + 8}px` }}
    >
      <FileCode2 size={13} className={clsx('shrink-0', active ? 'text-amber-300' : 'text-slate-500 group-hover:text-slate-400')} />
      <span className="min-w-0 flex-1 truncate">{node.name}</span>
      {active && editorDirty && (
        <span className="text-[10px] text-amber-300" title="Unsaved changes">
          ●
        </span>
      )}
      {node.version !== undefined && (
        <span className="rounded bg-white/[0.04] px-1.5 py-0.5 text-[10px] font-mono text-slate-400 group-hover:text-slate-300">
          v{node.version}
        </span>
      )}
    </button>
  )
}

function DirRow({
  node,
  depth,
  open,
  onToggle,
}: {
  node: TreeNode
  depth: number
  open: boolean
  onToggle: () => void
}) {
  return (
    <>
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-xs font-medium text-slate-400 transition-colors hover:bg-white/[0.04] hover:text-slate-200"
        style={{ paddingLeft: `${depth * 14 + 8}px` }}
      >
        {open ? <ChevronDown size={13} className="text-slate-500" /> : <ChevronRight size={13} className="text-slate-500" />}
        {open ? <FolderOpen size={13} className="text-amber-300/80" /> : <Folder size={13} className="text-amber-300/80" />}
        <span className="truncate">{node.name}</span>
      </button>
      {open &&
        node.children.map((child) => (
          <ExplorerNode key={child.path} node={child} depth={depth + 1} />
        ))}
    </>
  )
}

function ExplorerNode({ node, depth }: { node: TreeNode; depth: number }) {
  const [open, setOpen] = useState(true)
  if (node.children.length === 0) return <FileRow node={node} depth={depth} />
  return <DirRow node={node} depth={depth} open={open} onToggle={() => setOpen(!open)} />
}

export function FileExplorer() {
  const activeProjectId = useWorkspaceStore((s) => s.activeProjectId)
  const files = useWorkspaceStore((s) => s.files)
  const filesState = useWorkspaceStore((s) => s.filesState)
  const filesError = useWorkspaceStore((s) => s.filesError)
  const refreshFiles = useWorkspaceStore((s) => s.refreshFiles)
  const robotInstall = useWorkspaceStore((s) => s.robotInstall)
  const installRobot = useWorkspaceStore((s) => s.installRobot)

  const installing = robotInstall.status === 'loading'
  const tree = buildTree(files)

  return (
    <Panel
      title="Files"
      className="max-h-[55%] shrink-0"
      actions={
        activeProjectId ? (
          <ActionButton
            onClick={() => void refreshFiles()}
            disabled={filesState === 'loading'}
            variant="ghost"
            title="Refresh file list"
          >
            <RefreshCw size={12} />
          </ActionButton>
        ) : undefined
      }
    >
      <div className="space-y-0.5 p-2">
        {!activeProjectId && <p className="p-2.5 text-xs text-slate-500">Create a sequence to see its files.</p>}
        {activeProjectId && filesState === 'loading' && files.length === 0 && !installing && (
          <LoadingRow label="Loading files…" />
        )}
        {installing && <LoadingRow label="Installing MicroDuck…" />}
        {filesError && <ErrorNotice message={filesError} onRetry={() => void refreshFiles()} />}
        {robotInstall.status === 'error' && robotInstall.error && files.length === 0 && (
          <ErrorNotice
            message={`MicroDuck auto-install failed: ${robotInstall.error}`}
            onRetry={() => void installRobot(DEFAULT_ROBOT_ID)}
          />
        )}
        {tree.map((node) => (
          <ExplorerNode key={node.path} node={node} depth={0} />
        ))}
      </div>
    </Panel>
  )
}
