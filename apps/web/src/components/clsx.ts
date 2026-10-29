/** Tiny classnames helper (no extra dependency). */
export function clsx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}
