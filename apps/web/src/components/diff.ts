// Minimal line-based diff for showing model-proposed corrections.
// Small LCS over lines with a size cap; no dependency needed.

export interface DiffRow {
  kind: 'same' | 'add' | 'del'
  text: string
  oldNo: number | null
  newNo: number | null
}

const MAX_DIFF_LINES = 2000

export function diffLines(oldText: string, newText: string): DiffRow[] | null {
  const oldLines = oldText.split('\n')
  const newLines = newText.split('\n')
  if (oldLines.length + newLines.length > MAX_DIFF_LINES) return null

  const n = oldLines.length
  const m = newLines.length
  // LCS length table (full matrix is fine at this cap).
  const table: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      table[i][j] =
        oldLines[i] === newLines[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1])
    }
  }

  const rows: DiffRow[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (oldLines[i] === newLines[j]) {
      rows.push({ kind: 'same', text: oldLines[i], oldNo: i + 1, newNo: j + 1 })
      i++
      j++
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      rows.push({ kind: 'del', text: oldLines[i], oldNo: i + 1, newNo: null })
      i++
    } else {
      rows.push({ kind: 'add', text: newLines[j], oldNo: null, newNo: j + 1 })
      j++
    }
  }
  while (i < n) {
    rows.push({ kind: 'del', text: oldLines[i], oldNo: i + 1, newNo: null })
    i++
  }
  while (j < m) {
    rows.push({ kind: 'add', text: newLines[j], oldNo: null, newNo: j + 1 })
    j++
  }
  return rows
}

/** Keep changed hunks with a few context lines so long files stay readable. */
export function changedHunks(rows: DiffRow[], context = 3): DiffRow[] {
  const keep = new Array<boolean>(rows.length).fill(false)
  rows.forEach((row, idx) => {
    if (row.kind === 'same') return
    for (let k = Math.max(0, idx - context); k <= Math.min(rows.length - 1, idx + context); k++) {
      keep[k] = true
    }
  })
  return rows.filter((_, idx) => keep[idx])
}
