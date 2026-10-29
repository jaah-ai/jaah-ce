/* A git-like text diff for the console's comparison views (`ui/TextDiffDialog.tsx`): lines first, then the
 * changed WORDS inside each modified line, so a one-word edit in a long paragraph is findable at a glance.
 *
 * Pure and dependency-free: an LCS over the middle left after trimming the common prefix and suffix, which
 * is where every real edit's cost goes. A middle too large for the table (`MAX_CELLS`) degrades to "the
 * whole middle was replaced" — correct, merely coarse — rather than freezing the tab. */

export type Segment = { text: string; changed: boolean }

/* One side of a row: its 1-based line number and its text, split where the other side differs. */
export type DiffCell = { no: number; segments: Segment[] }

/* A row of the side-by-side view. `before`/`after` absent = no line on that side (a pure insert/delete). */
export type DiffRow =
  | { kind: 'same'; before: DiffCell; after: DiffCell }
  | { kind: 'change'; before?: DiffCell; after?: DiffCell }

/* Consecutive unchanged rows beyond the context window, folded until the reader opens them. */
export type DiffBlock =
  | { kind: 'rows'; rows: DiffRow[] }
  | { kind: 'fold'; rows: DiffRow[] }

const MAX_CELLS = 4_000_000

type Op = { kind: 'same' | 'del' | 'ins'; a?: number; b?: number }

/** The edit script turning `a` into `b`, as indices into each. */
function lcsOps<T>(a: readonly T[], b: readonly T[]): Op[] {
  let start = 0
  while (start < a.length && start < b.length && a[start] === b[start]) start++
  let endA = a.length
  let endB = b.length
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB-- }

  const ops: Op[] = []
  for (let i = 0; i < start; i++) ops.push({ kind: 'same', a: i, b: i })

  const n = endA - start
  const m = endB - start
  if (n > 0 && m > 0 && (n + 1) * (m + 1) <= MAX_CELLS) {
    // table[i][j] = LCS length of a[start+i..endA) and b[start+j..endB), flattened.
    const w = m + 1
    const table = new Uint32Array((n + 1) * w)
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        table[i * w + j] = a[start + i] === b[start + j]
          ? table[(i + 1) * w + j + 1] + 1
          : Math.max(table[(i + 1) * w + j], table[i * w + j + 1])
      }
    }
    let i = 0
    let j = 0
    while (i < n && j < m) {
      if (a[start + i] === b[start + j]) { ops.push({ kind: 'same', a: start + i, b: start + j }); i++; j++ }
      // Deletions before insertions within a change, the order a reader expects.
      else if (table[(i + 1) * w + j] >= table[i * w + j + 1]) { ops.push({ kind: 'del', a: start + i }); i++ }
      else { ops.push({ kind: 'ins', b: start + j }); j++ }
    }
    for (; i < n; i++) ops.push({ kind: 'del', a: start + i })
    for (; j < m; j++) ops.push({ kind: 'ins', b: start + j })
  } else {
    for (let i = start; i < endA; i++) ops.push({ kind: 'del', a: i })
    for (let j = start; j < endB; j++) ops.push({ kind: 'ins', b: j })
  }

  for (let k = 0; k < a.length - endA; k++) ops.push({ kind: 'same', a: endA + k, b: endB + k })
  return ops
}

/* Words, runs of whitespace and single punctuation marks: fine enough that "cat," → "dog," marks only the word. */
function tokenize(line: string): string[] {
  return line.match(/\s+|[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu) ?? []
}

function pushSegment(out: Segment[], text: string, changed: boolean) {
  const last = out[out.length - 1]
  if (last && last.changed === changed) last.text += text
  else out.push({ text, changed })
}

/** The changed words of a modified line, on each side. */
export function diffWords(before: string, after: string): { before: Segment[]; after: Segment[] } {
  const a = tokenize(before)
  const b = tokenize(after)
  const ops = lcsOps(a, b)
  // A lone space kept between two replacements reads as noise ("Second⎵draft" marked as two words): fold it
  // into the change on both sides, so a replaced phrase is marked as one. Only between runs that each both
  // delete and insert — otherwise the space would be marked on a side that never changed there.
  const replaces = (from: number, step: number) => {
    let del = false
    let ins = false
    for (let k = from; k >= 0 && k < ops.length && ops[k].kind !== 'same'; k += step) {
      if (ops[k].kind === 'del') del = true
      else ins = true
    }
    return del && ins
  }
  for (let k = 1; k < ops.length - 1; k++) {
    const op = ops[k]
    if (op.kind === 'same' && /^\s+$/.test(a[op.a!]) && replaces(k - 1, -1) && replaces(k + 1, 1)) {
      ops.splice(k, 1, { kind: 'del', a: op.a }, { kind: 'ins', b: op.b })
      k++
    }
  }
  const outA: Segment[] = []
  const outB: Segment[] = []
  for (const op of ops) {
    if (op.kind === 'same') { pushSegment(outA, a[op.a!], false); pushSegment(outB, b[op.b!], false) }
    else if (op.kind === 'del') pushSegment(outA, a[op.a!], true)
    else pushSegment(outB, b[op.b!], true)
  }
  return { before: outA, after: outB }
}

/* One trailing newline ends the last line rather than starting an empty one. */
function splitLines(text: string): string[] {
  const normal = text.replace(/\r\n?/g, '\n').replace(/\n$/, '')
  return normal === '' ? [] : normal.split('\n')
}

/* Word marks cost an LCS per modified pair; past this many characters in all, the remaining pairs are marked
 * whole-line, so one pathological edit cannot freeze the tab. */
const WORD_BUDGET = 200_000

/** The side-by-side rows turning `before` into `after`. A run of deletions followed by insertions is a
 * modification: its lines pair up row by row and get word-level marks; the surplus on either side stands
 * alone opposite an empty cell. */
export function diffRows(before: string, after: string): DiffRow[] {
  const a = splitLines(before)
  const b = splitLines(after)
  const rows: DiffRow[] = []
  let wordBudget = WORD_BUDGET
  let dels: number[] = []
  let ins: number[] = []

  const flush = () => {
    const paired = Math.min(dels.length, ins.length)
    for (let k = 0; k < Math.max(dels.length, ins.length); k++) {
      const ai = dels[k]
      const bi = ins[k]
      if (k < paired) {
        wordBudget -= a[ai].length + b[bi].length
        const words = wordBudget >= 0
          ? diffWords(a[ai], b[bi])
          : { before: [{ text: a[ai], changed: true }], after: [{ text: b[bi], changed: true }] }
        rows.push({
          kind: 'change',
          before: { no: ai + 1, segments: words.before },
          after: { no: bi + 1, segments: words.after },
        })
      } else if (ai !== undefined) {
        rows.push({ kind: 'change', before: { no: ai + 1, segments: [{ text: a[ai], changed: false }] } })
      } else {
        rows.push({ kind: 'change', after: { no: bi + 1, segments: [{ text: b[bi], changed: false }] } })
      }
    }
    dels = []
    ins = []
  }

  for (const op of lcsOps(a, b)) {
    if (op.kind === 'del') {
      // A deletion after insertions starts a new change block.
      if (ins.length > 0) flush()
      dels.push(op.a!)
    } else if (op.kind === 'ins') {
      ins.push(op.b!)
    } else {
      flush()
      const text = a[op.a!]
      rows.push({
        kind: 'same',
        before: { no: op.a! + 1, segments: [{ text, changed: false }] },
        after: { no: op.b! + 1, segments: [{ text, changed: false }] },
      })
    }
  }
  flush()
  return rows
}

/** `rows` with every unchanged run longer than `2 × context` folded down to `context` lines either side of
 * a change (none before the first change or after the last). */
export function foldRows(rows: DiffRow[], context = 3): DiffBlock[] {
  const blocks: DiffBlock[] = []
  let i = 0
  while (i < rows.length) {
    if (rows[i].kind === 'change') {
      let j = i
      while (j < rows.length && rows[j].kind === 'change') j++
      blocks.push({ kind: 'rows', rows: rows.slice(i, j) })
      i = j
      continue
    }
    let j = i
    while (j < rows.length && rows[j].kind === 'same') j++
    const run = rows.slice(i, j)
    const lead = i === 0 ? 0 : context
    const trail = j === rows.length ? 0 : context
    // Folding fewer than two lines saves nothing: the fold bar is a line itself.
    if (run.length - lead - trail >= 2) {
      if (lead > 0) blocks.push({ kind: 'rows', rows: run.slice(0, lead) })
      blocks.push({ kind: 'fold', rows: run.slice(lead, run.length - trail) })
      if (trail > 0) blocks.push({ kind: 'rows', rows: run.slice(run.length - trail) })
    } else {
      blocks.push({ kind: 'rows', rows: run })
    }
    i = j
  }
  return blocks
}

/** Lines removed and added, as git's `--stat` counts them. */
export function diffStats(rows: DiffRow[]): { removed: number; added: number } {
  let removed = 0
  let added = 0
  for (const r of rows) {
    if (r.kind !== 'change') continue
    if (r.before) removed++
    if (r.after) added++
  }
  return { removed, added }
}
