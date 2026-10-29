// Pure helpers for the project docs reader (`pages/ProjectDocsPage.tsx`): path grouping and relative-link
// resolution, unit-testable without rendering.

export interface DocsGroup {
  /** Directory the documents sit in, repo-relative. '' for the docs root. */
  dir: string
  /** Full repo-relative paths, sorted. */
  paths: string[]
}

/**
 * Group a flat path list into one section per directory. Grouped by (dir, filename), NOT by sorting whole
 * paths: `/` sorts below every letter, so `x/a/README.md` would precede `x/b.md` and
 * interleave the sections.
 */
export function groupByDir(paths: string[]): DocsGroup[] {
  const byDir = new Map<string, string[]>()
  for (const path of paths) {
    const dir = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : ''
    const bucket = byDir.get(dir)
    if (bucket) bucket.push(path)
    else byDir.set(dir, [path])
  }
  return [...byDir.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([dir, dirPaths]) => ({ dir, paths: [...dirPaths].sort((a, b) => a.localeCompare(b)) }))
}

/** The sidebar label: filename minus `.md`; a directory's `README.md` / `index.md` gets a name saying
 *  what it is. */
export function docLabel(path: string): string {
  const file = path.slice(path.lastIndexOf('/') + 1)
  const stem = file.replace(/\.md$/i, '')
  return stem === 'README' || stem === 'index' ? 'Overview' : stem
}

/** Resolve a link in `fromPath` against the repo root, collapsing `.`/`..`; null if it climbs out. */
export function resolveRelative(fromPath: string, href: string): string | null {
  const base = fromPath.includes('/') ? fromPath.slice(0, fromPath.lastIndexOf('/')).split('/') : []
  const out = href.startsWith('/') ? [] : [...base]
  for (const part of href.replace(/^\//, '').split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') {
      if (out.length === 0) return null
      out.pop()
    } else out.push(part)
  }
  return out.join('/') || null
}

export interface LinkTarget {
  kind: 'doc' | 'external'
  /** For 'doc', the repo-relative path to open. For 'external', the URL to follow. */
  target: string
}

/**
 * Where a link inside a rendered document goes: a markdown file in `paths` (the authoritative set)
 * navigates INSIDE the reader; everything else goes to GitHub (better rendering, GitHub's own auth).
 */
export function linkTarget(
  fromPath: string,
  href: string,
  paths: Set<string>,
  blobBase: string,
): LinkTarget {
  if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('//')) {
    return { kind: 'external', target: href }
  }
  const [pathPart, hash] = href.split('#')
  if (!pathPart) return { kind: 'external', target: href } // a bare '#anchor' — same page
  const resolved = resolveRelative(fromPath, pathPart)
  if (resolved === null) return { kind: 'external', target: href }
  if (paths.has(resolved)) {
    return { kind: 'doc', target: hash ? `${resolved}#${hash}` : resolved }
  }
  // ONLY A RELATIVE LINK EARNS A BLOB URL: an absolute FILESYSTEM path (`/usr/lib/jaahd/docs/...`) after
  // the blob base would 404; left alone it is inert, as on GitHub.
  if (pathPart.startsWith('/')) return { kind: 'external', target: href }
  return { kind: 'external', target: `${blobBase}/${resolved}` }
}

/**
 * Drop HTML comments (this repo's invisible markers, `<!-- directive:branching -->` …): with HTML
 * disabled the reader would show them as literal text mid-paragraph.
 * FENCED CODE IS LEFT ALONE (there the marker is the subject), which needs a line-by-line fence scan.
 */
export function stripHtmlComments(markdown: string): string {
  const out: string[] = []
  let inFence = false
  let inComment = false
  for (const line of markdown.split('\n')) {
    if (/^\s{0,3}(```|~~~)/.test(line)) {
      inFence = !inFence
      out.push(line)
      continue
    }
    if (inFence) {
      out.push(line)
      continue
    }
    let text = line
    if (inComment) {
      const close = text.indexOf('-->')
      if (close === -1) continue
      text = text.slice(close + 3)
      inComment = false
    }
    text = text.replace(/<!--[\s\S]*?-->/g, '')
    const open = text.indexOf('<!--')
    if (open !== -1) {
      inComment = true
      text = text.slice(0, open)
    }
    if (text.trim() !== '' || line.trim() === '') out.push(text)
  }
  return out.join('\n')
}
