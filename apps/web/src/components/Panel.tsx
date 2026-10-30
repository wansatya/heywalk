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
      <header className="flex min-h-[40px] shrink-0 items-center justify-between gap-2.5 border-b border-border-subtle/80 bg-sidebar/70 px-3.5 py-2">
        <h2 className="select-none text-[11px] font-semibold uppercase tracking-wider text-stone-500">
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
      ? 'bg-amber-600 text-white font-semibold shadow-sm hover:bg-amber-500 disabled:bg-amber-200 disabled:text-amber-400 active:scale-[0.98]'
      : variant === 'danger'
        ? 'bg-red-50 text-red-700 border border-red-200 hover:bg-red-100 disabled:opacity-40 active:scale-[0.98]'
        : variant === 'ghost'
          ? 'text-stone-600 hover:text-stone-900 hover:bg-stone-200/60 disabled:opacity-40 active:scale-[0.98]'
          : 'bg-white text-stone-700 border border-border-subtle hover:bg-stone-50 hover:text-stone-900 disabled:opacity-40 shadow-sm active:scale-[0.98]'
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
    <div className="m-3 rounded-lg border border-red-200 bg-red-50 p-3.5 text-xs leading-relaxed text-red-700 shadow-sm">
      <p className="break-words">{message}</p>
      {hint && <p className="mt-1.5 break-words text-amber-700 font-sans">Hint: {hint}</p>}
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-2.5 rounded-md border border-red-200 bg-red-100 px-2.5 py-1 text-xs font-medium hover:bg-red-200 transition-colors"
        >
          Retry
        </button>
      )}
    </div>
  )
}

export function LoadingRow({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2.5 px-3.5 py-2.5 text-xs font-medium text-stone-500">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-stone-400 border-t-transparent" />
      {label}
    </div>
  )
}
