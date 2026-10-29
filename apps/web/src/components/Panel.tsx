import type { ReactNode } from 'react'
import { clsx } from './clsx'

export function Panel({
  title,
  actions,
  children,
  className,
  bodyClassName,
}: {
  title: string
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
}) {
  return (
    <section
      className={clsx(
        'flex min-h-0 flex-col overflow-hidden rounded-xl border border-border-subtle bg-elevated shadow-sm',
        className,
      )}
    >
      <header className="flex min-h-[40px] shrink-0 items-center justify-between gap-2.5 border-b border-border-subtle/80 bg-elevated/70 px-3.5 py-2">
        <h2 className="select-none text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          {title}
        </h2>
        {actions && <div className="flex items-center gap-1.5">{actions}</div>}
      </header>
      <div className={clsx('min-h-0 flex-1 overflow-y-auto', bodyClassName)}>{children}</div>
    </section>
  )
}

export function ActionButton({
  children,
  onClick,
  disabled,
  title,
  variant = 'default',
}: {
  children: ReactNode
  onClick?: () => void
  disabled?: boolean
  title?: string
  variant?: 'default' | 'primary' | 'danger' | 'ghost'
}) {
  const styles =
    variant === 'primary'
      ? 'bg-amber-400 text-slate-950 font-semibold shadow-sm hover:bg-amber-300 disabled:bg-amber-400/30 disabled:text-slate-600 active:scale-[0.98]'
      : variant === 'danger'
        ? 'bg-red-500/15 text-red-200 border border-red-500/30 hover:bg-red-500/25 disabled:opacity-40 active:scale-[0.98]'
        : variant === 'ghost'
          ? 'text-slate-300 hover:text-white hover:bg-white/[0.06] disabled:opacity-40 active:scale-[0.98]'
          : 'bg-white/[0.04] text-slate-200 border border-border-subtle hover:bg-white/[0.08] hover:text-white disabled:opacity-40 shadow-sm active:scale-[0.98]'
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={clsx(
        'inline-flex items-center justify-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium transition-all disabled:cursor-not-allowed',
        styles,
      )}
    >
      {children}
    </button>
  )
}

export function ErrorNotice({
  message,
  hint,
  onRetry,
}: {
  message: string
  hint?: string | null
  onRetry?: () => void
}) {
  return (
    <div className="m-3 rounded-lg border border-red-500/30 bg-red-500/10 p-3.5 text-xs leading-relaxed text-red-200 shadow-sm">
      <p className="break-words">{message}</p>
      {hint && <p className="mt-1.5 break-words text-amber-200/90 font-sans">Hint: {hint}</p>}
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-2.5 rounded-md border border-red-500/40 bg-red-500/20 px-2.5 py-1 text-xs font-medium hover:bg-red-500/30 transition-colors"
        >
          Retry
        </button>
      )}
    </div>
  )
}

export function LoadingRow({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2.5 px-3.5 py-2.5 text-xs font-medium text-slate-400">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-500 border-t-transparent" />
      {label}
    </div>
  )
}
