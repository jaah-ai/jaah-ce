/* A NAMED ROSTER'S TREE SHAPE: the rule turning a row's `/`-separated `name` into Tree mode's outline
 * (requirements R3.2, R3.3 live in the Credentials epic). Shared by every roster that files rows into
 * folders by name — Credentials (`pages/credentials/credentialTree.ts`) and Playbooks. `name` is the only carrier of structure (no parent column or
 * folder entity), so the hierarchy is a VIEW; a runner still resolves a credential by its full name.
 * Pure (the `lib/docsTree.ts` precedent): every name shape has a settled answer tested in
 * `credentialTree.test.ts`.
 * ORDERING: arrival order all the way down (server `created_at DESC, id DESC`); the renderer must NOT
 * re-sort. A group takes its position from its FIRST member, so groups and root leaves interleave
 * (R3.3(b)), and a later member appends without moving it. An alphabetical sort of one server page would
 * look like a roster missing letters.
 * No synthetic nodes ("Other", "ungrouped"). A leaf and a group may be SIBLINGS UNDER ONE LABEL (`jira`
 * beside `jira/prod`, R3.3(b)); the renderer tells them apart by affordance, never by label. */
/** The separator's single home. */
export const NAME_PATH_SEPARATOR = '/'

/** What a row must carry to be filed in a tree. `org_id` (absent = no org) feeds the one-tenant drop
 *  rule (`nameTreeMove.ts`); `editable` (absent = the caller's key decides) the folder-lift rule. */
export type NamedRow = {
  id: string
  name: string
  org_id?: string | null
  editable?: boolean | null
}

/**
 * A discriminated union (the `components/tasks/taskTree.ts` precedent): a group has no row, a leaf has no
 * children.
 */
export type NameTreeNode<R extends NamedRow> =
  | {
      kind: 'group'
      /**
       * The group's STABLE IDENTITY: full path of NORMALISED segments (`a`, `a/b`, …). A path, not an index,
       * because the persisted expansion `Set` is keyed by it and must survive paging, re-sorts and filters.
       * `a//b`, `a / b` and `a/b` are one group.
       */
      path: string
      /** The last segment of `path`. Never blank. */
      label: string
      /** Arrival-ordered groups and leaves. Never empty. */
      children: NameTreeNode<R>[]
    }
  | {
      kind: 'leaf'
      /**
       * The name's last non-empty segment. Falls back to the full RAW name when there is none (R3.3(e): `/`,
       * `///`), so the leaf is never blank; an empty/whitespace name is refused by the API (400).
       */
      label: string
      /**
       * The row, and the leaf's whole identity — a leaf carries NO path. Names are not unique (and that
       * constraint must not be added), so a path must never become a React key; `row.id` is unique. Telling
       * duplicates apart on screen is the renderer's half of R3.3(d).
       */
      row: R
    }

/**
 * `a//b/` and `a / b` → `['a', 'b']`: trim each segment, then drop empties (R3.3(c)). The trim matters
 * as much as the drop: the server trims only OUTER whitespace, and an untrimmed `a ` would be a second,
 * identical-looking group.
 */
export function nameSegments(name: string): string[] {
  return name
    .split(NAME_PATH_SEPARATOR)
    .map((segment) => segment.trim())
    .filter((segment) => segment !== '')
}

/**
 * Build the Tree-mode outline for a roster page. Total over every name shape (R3.3 (a)–(e)); never drops
 * a row — leaf count always equals `rows.length`.
 */
export function buildNameTree<R extends NamedRow>(rows: readonly R[]): NameTreeNode<R>[] {
  const roots: NameTreeNode<R>[] = []
  // Every group by full path, so a later row re-enters the FIRST row's group.
  const groups = new Map<string, Extract<NameTreeNode<R>, { kind: 'group' }>>()

  for (const row of rows) {
    const segments = nameSegments(row.name)
    // No segment at all: a ROOT LEAF with the raw name, never filed under an invented parent.
    const label = segments.length > 0 ? segments[segments.length - 1] : row.name
    const groupSegments = segments.slice(0, -1)

    let siblings = roots
    let path = ''
    for (const segment of groupSegments) {
      path = path === '' ? segment : `${path}${NAME_PATH_SEPARATOR}${segment}`
      let group = groups.get(path)
      if (!group) {
        group = { kind: 'group', path, label: segment, children: [] }
        groups.set(path, group)
        // Appended where its first member found it: its arrival position.
        siblings.push(group)
      }
      siblings = group.children
    }

    // ALWAYS A PUSH, NEVER AN UPSERT (R3.3(d)): an identical name lands a second leaf. Keep it structural.
    siblings.push({ kind: 'leaf', label, row })
  }

  return roots
}

/** WHAT A ROSTER CALLS ITS OWN ROWS, and the one limit its server puts on a name — everything the shared
 *  tree modules say or refuse that differs per roster. One object per roster (`CREDENTIAL_TREE_KIND`,
 *  `PLAYBOOK_TREE_KIND`), so the copy and the DOM oracles cannot drift between its builders. */
export type NameTreeKind = {
  /** The row noun, singular and plural: "credential"/"credentials". */
  noun: { one: string; many: string }
  /** The server's name cap (measured trimmed), mirrored so an over-long drop is refused before the
   *  gesture; `null` when the server sets none. The server still decides. */
  nameMax: number | null
  /** The prefix of every `data-*` / `data-testid` handle the tree rows carry (`credential-group`, …). */
  idPrefix: string
  /** `false` draws a group row with no mark (an empty disc slot): its disclosure and count already say it is a
   *  folder, and its accessible name says "group". Absent = the `◈` mark. */
  groupMark?: boolean
}
