export function SimViewportFallback() {
  return (
    <div className="flex h-full w-full items-center justify-center gap-2.5 bg-base p-6 text-xs font-medium text-stone-400">
      <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-stone-300 border-t-transparent" />
      Loading simulation view…
    </div>
  )
}
