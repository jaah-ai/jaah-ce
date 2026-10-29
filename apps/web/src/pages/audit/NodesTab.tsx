import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { get } from '../../api'
import { useCan } from '../../auth'
import { useApiRead } from '../../hooks/useApiRead'
import { useOpenDetailPath } from '../../components/ListDetail'
import { isOpenPath } from '../../lib/listDetail'
import { FilterSelect, vocabularyOptions } from '../../ui/FilterRow'
import { useLiveRefetch } from '../../hooks/useLiveRefetch'
import { useEventStream } from '../../hooks/useEventStream'
import { useNodesRoster, workNodes } from '../../hooks/useNodesRoster'
import { shortNode } from '../../lib/nodeName'
import { absoluteTime, compactAge } from '../../lib/relativeTime'
import type { Command, CommandPage } from '../../types'
import Banner from '../../ui/Banner'
import NoMatchesState from '../../ui/NoMatchesState'
import { AGE_SLOT } from '../../ui/feed/geometry'
import type { FeedRowSpec } from '../../ui/feed/types'
import StatusChip from '../../ui/StatusChip'
import { PageNav } from '../../ui/Pager'
import { usePageClamp } from '../../hooks/usePageClamp'
import { toneFor } from '../../ui/statusTone'
import { PER_PAGE, setRangeParams, usePagedLogFilters } from '../../hooks/usePagedLogFilters'
import ListStates from '../../ui/feed/ListStates'
import PagedLogView from './PagedLogView'
import { UNBOUND_NODE_LABEL, commandPath } from './CommandDetailPage'

function targetLabel(cmd: Command): string {
  const a = cmd.args
  if (typeof a['project'] === 'string') {
    const proj = a['project']
    if (typeof a['number'] === 'number') return `${proj} #${a['number']}`
    if (typeof a['issue'] === 'number') return `${proj} issue #${a['issue']}`
    return proj
  }
  return JSON.stringify(a)
}


/* THE "BOUND TO NO NODE" OPTION'S WIRE VALUE, mirroring `routes/portfolio.py`'s `UNBOUND_NODE`, which
   is the single home for why the token is spelled with an `@`: `node_id` accepts a node uuid, and `@`
   never occurs in one, so the sentinel cannot collide with a real one. */
const UNBOUND_NODE = '@none'

/* THE STATUS VOCABULARY, and it is the CLIENT that decides which values are worth offering — the route
   deliberately validates none, because `status` is whatever the node's own reply carried
   (`routes/commands.py`) and the set is open by construction. These four are what the table actually
   holds, counted on dev: done 10,687 · failed 4,879 · rejected 3,060 · dispatched 759. OFFERING
   `dispatched` IS WHY `statusToken` BELOW LEARNED IT: 759 rows an operator could not ask for was the
   defect, and answering that ask with 759 red `failed` marks would have been a control whose results
   contradict themselves. Its tone is `ui/statusTone.ts`'s to decide and it is neutral there. */
const STATUS_OPTIONS = [
  { value: 'done', label: 'done' },
  { value: 'failed', label: 'failed' },
  { value: 'rejected', label: 'rejected' },
  { value: 'dispatched', label: 'dispatched' },
]

/* Dispatch is synchronous, so an audit-log row is normally already terminal — no more
 * queued/running/cancelled. `dispatched` IS THE ONE NON-TERMINAL SURVIVOR, and it belongs in this set
 * rather than in the `failed` catch-all: `routes/commands.py` writes it when a dispatch got no reply
 * inside the timeout, and its own comment says why — "the outcome is unknown, so the status stays
 * `dispatched` rather than a guess". Painting it red asserts the guess the writer refused to make.
 *
 * IT IS HERE BECAUSE OF THE STATUS FILTER BESIDE IT. dev holds 759 of these rows, and offering
 * `dispatched` as a filter option (which is the point — 759 rows an operator cannot ask for is the
 * defect) while rendering every one of them as a red `failed` mark would ship a control whose results
 * contradict themselves. `badge-dispatched` resolves through `ui/statusTone.ts` like every other token;
 * an unrecognised status still reads `failed`, which is the safe direction for a value nothing here
 * knows. */
function statusToken(status: string): string {
  return status in { done: 1, failed: 1, rejected: 1, dispatched: 1 } ? status : 'failed'
}

/* THE OUTCOME'S SHAPE ON THE ROW'S STATE DISC. The TONE is still `ui/statusTone`'s; the glyph it does
 * not carry is what `FeedIndicator` requires. Four shapes, read apart at 11px: a tick for a command the
 * node ran, a cross for one it ran and failed, a slashed ring for one it refused before running, and the
 * feed's in-flight chevron for `dispatched` — "sent, and we never heard back", the one non-terminal
 * state and the one the tone map leaves grey. */
const STATUS_GLYPH: Record<string, string> = {
  done: '✓',
  failed: '✕',
  rejected: '⊘',
  dispatched: '»',
}

/* The Audit page's NODES tab — what `pages/CommandLog.tsx` was, moved here whole.
 *
 * IT ARRIVED HERE AS A MOVE and it is still that page: the same node-grant-scoped
 * `GET /api/command-log`, the same `useEventStream('/api/stream', ['command'])` live refresh with its
 * 150/1000 ms coalescing and its 15 s stream-down fallback poll. A row opens its command's detail page
 * (`CommandDetailPage.tsx`, the args and result JSON) beside the log. It has
 * no page-level `<PageHeader>`: the sidebar and the shell's (`AuditPage`) tab strip name the page.
 *
 * THE NINE COLUMNS ARE NOW A FEED ROW — see the row builder below. That deletes this
 * panel's ENTIRE responsive answer: a nine-area `grid-template-areas` phone card, a per-cell `caption`
 * twin of the header, a fixed column budget and the `tableLayout` switch. `ui/feed/FeedList` drops slots
 * as the viewport narrows, so there is no second layout here to keep in step.
 *
 * WHAT IS NO LONGER A MOVE IS THE FILTERING, and the reason is that the incumbent's
 * was WRONG rather than merely thin. The page shipped with one Node picker whose options were the nodes
 * present in the fifty rows currently loaded and whose filter was a `.filter()` over those same fifty:
 * on the dev control plane, selecting `aws2` rendered 2 rows of that node's 239, and nothing on screen
 * said the answer was partial. `GET /api/command-log` had taken a server-side `node_id` all along —
 * this, its only caller, simply never passed one. So every filter now travels as a query parameter, the
 * options come from the fleet rather than from the page, the endpoint returns a filtered `total`, and
 * the search box, the status filter and the date range that the sibling Activity tab has always had are
 * here too. The epic's ask was "browsable, searchable, filterable"; this half of it was only browsable.
 *
 * `/command-log` is now a REDIRECT onto `/audit/nodes` (App.tsx), so this tab is the only thing
 * that renders the command log; the wrapper page that route used to have is deleted. The redirect stays
 * because bookmarks still name the old path. */
export default function NodesTab() {
  /* EVERY ONE OF THESE IS A QUERY PARAMETER, and that IS the fix. What this page had was
     `commands.filter(c => c.node_id === nodeFilter)` — a pass over the fifty rows already in memory —
     while `GET /api/command-log` had taken a server-side `node_id` all along and this, its only caller,
     never passed one. `query` is the debounced value that actually travels (`ui/SearchBox` holds the live
     text), so a fast typist costs one request rather than one per keystroke. */
  const {
    zone, query, from, to, page, setPage, narrow, clearShared, sharedFiltersSet, searchFields, rangeFields,
  } = usePagedLogFilters(1)
  const [nodeFilter, setNodeFilter] = useState('')
  const [status, setStatus] = useState('')

  const navigate = useNavigate()
  const openPath = useOpenDetailPath()

  /* `query`, the term the page is filtered BY: a filtered-empty page never renders "No commands yet"
     beside no way to clear what filtered it. */
  const hasFilters = Boolean(sharedFiltersSet || nodeFilter || status)

  /* EVERY FILTER CHANGE RESETS THE PAGE (`narrow`): a narrowed result set that still has 3 pages
     would leave the reader on a page that no longer exists, staring at an empty table. */
  function clearFilters() {
    narrow(() => {
      clearShared()
      setNodeFilter('')
      setStatus('')
    })
  }

  /* CLEARED MEANS OMITTED — the API normalises an empty string for the three text parameters but
     REFUSES an empty typed one (`?start=` is a 422 before the handler is entered), so a cleared filter
     drops its key rather than sending it blank. Doing it uniformly for all five is one rule instead of
     two. */
  const url = useMemo(() => {
    const params = new URLSearchParams()
    params.set('page', String(page))
    params.set('per', String(PER_PAGE))
    if (query) params.set('q', query)
    if (nodeFilter) params.set('node_id', nodeFilter)
    if (status) params.set('status', status)
    setRangeParams(params, from, to)
    return `/api/command-log?${params.toString()}`
  }, [page, query, nodeFilter, status, from, to])

  /* Initial load, every page change and every filter change all go through REST (spec R3), keyed on `url`.
     A superseded response is discarded by the hook's generation fence (a fast typist plus the debounce). */
  const { data, loading, error, refresh } = useApiRead(
    ['nodes-command-log', url],
    () => get<CommandPage>(url),
    'Failed to load command log',
  )
  const items: Command[] = data?.items ?? []
  /* THE FILTERED COUNT, straight off the wire — the one thing the old envelope did not carry and the
     reason the pager below could not be honest. */
  const total = data?.total ?? 0

  // Scoped to the entity this page renders. `command` events did not exist until
  // `routes/commands.py::_record_audit` started emitting one, so this page had nothing of its own to
  // listen for and refetched on every entity instead — including all of `task`'s emit sites.
  const commandStream = useEventStream('/api/stream', ['command'])

  // Primary refresh trigger: a `command` event re-fetches the CURRENT VIEW — the same `url`, filters
  // and page included, so a live arrival cannot silently widen what the operator asked for.
  //
  // THE LIVE TAIL AND OFFSET PAGING SKEW AGAINST EACH OTHER, AND THAT IS A DECISION HERE RATHER THAN AN
  // OVERSIGHT. The sibling Activity tab pages by cursor so the list cannot shift under the reader between
  // pages, and pays for it by having NO live tail at all. This tab keeps
  // the live tail — it is the property `e2e/audit-tabs.spec.ts` pins as AC6.4, and watching a dispatch
  // land without reloading is most of what the page is for — so on page 2 or later, two commands
  // arriving shift the window down by two. It is the behaviour this page has always had and it is
  // bounded by the log's own rate (a few hundred rows a day). NAMED FOLLOW-UP, deliberately not taken
  // under this child's scope: a `pinned_at` upper bound on `GET /api/command-log`, re-pinned on every
  // filter change, which would let the tab have both.
  // `_record_audit` is `command_audit_log`'s only writer and runs on every dispatch, so any command that
  // also moved a worktree wrote a row here too and fires this event; the row is written once and
  // `completed_at` is never updated, so there is no later mutation to miss. Stream down -> 15s poll.
  useLiveRefetch(commandStream, () => void refresh(), 15000)

  function resultSummary(cmd: Command): string {
    if (!cmd.result) return '—'
    if (typeof cmd.result['reason'] === 'string') return cmd.result['reason']
    if (typeof cmd.result['exit_code'] === 'number') {
      return `exit ${cmd.result['exit_code']}`
    }
    return '(see detail)'
  }

  /* node_id -> presence status, for greying a row whose node has gone OFFLINE. Gated on the
     roster's own key — `GET /api/nodes` declares `nodes:view`, not `system:view` — so a `system:view`-only
     reader gets an empty lookup instead of a 403 per event and poll. */
  const nodesQuery = useNodesRoster(useCan()('nodes:view'))
  const nodeStatus = useMemo(
    () => Object.fromEntries((nodesQuery.data ?? []).map(n => [n.node_id, n])),
    [nodesQuery.data],
  )
  // uuid -> display name, for the filter: a presence-only host (`id: null`) has no uuid to filter on, and a
  // Multi-Node Host is not offered (`workNodes`).
  const nodeNames = useMemo(
    () => Object.fromEntries(workNodes(nodesQuery.data ?? []).flatMap(n => (n.id ? [[n.id, n.name]] : []))) as Record<string, string>,
    [nodesQuery.data],
  )

  /* THE OPTIONS ARE THE FLEET, NOT THE PAGE. This used to be
     `Array.from(new Set(commands.map(c => c.node_id)))` — the nodes that happened to appear in the
     fifty rows currently loaded — so a node with no command on page 1 could not be selected at all, and
     the rows bound to NO node (561 of them on dev) were unrepresentable. `nodesQuery` already holds
     the whole roster this page can see, node-grant scoped by `GET /api/nodes` exactly as the log itself
     is, so the options are complete and cost no extra request. */
  const nodeOptions = useMemo(
    () => [
      /* A NODE DEREGISTERED WHILE IT IS SELECTED KEEPS ITS OPTION. `nodesQuery` refetches on every
         `node` event, so the roster can lose the node this control is set to — and a native `<select>`
         whose value matches no option renders BLANK while the state goes on filtering by it, which is
         the control and the result disagreeing about why the table is empty. Its rows outlive it
         (`ON DELETE SET NULL` is what the `No node` option exists for), so the honest thing is to keep
         offering it (by uuid) until the operator changes it. */
      ...vocabularyOptions(
        Object.keys(nodeNames).sort((a, b) => nodeNames[a].localeCompare(nodeNames[b])),
        nodeFilter === UNBOUND_NODE ? '' : nodeFilter,
        // The value is the uuid (`?node_id=` is uuid-only); the label its name, or the uuid once the node is gone.
        { label: uuid => nodeNames[uuid] ?? uuid },
      ),
      { value: UNBOUND_NODE, label: UNBOUND_NODE_LABEL },
    ],
    [nodeNames, nodeFilter],
  )

  /* THE ONLY WAY TO BE STRANDED PAST THE LAST PAGE, since every filter change routes through `narrow`
     and resets to 1: the result set SHRINKS underneath the reader — a live refetch after a node is
     deleted, say — leaving "Page 5 of 2" beside a disabled Next and no way forward. The hook clamps back. */
  const totalPages = usePageClamp(page, setPage, total, PER_PAGE)

  /* THE LOG AS FEED ROWS. Status is the MARK (`ui/statusTone`'s tone, this file's `STATUS_GLYPH` shape),
     the WORD staying on the disc's `title`, at the head of the accessible name and on `data-status`.
     Type and target are ONE title — WHAT was run and WHAT against — with the full string on the row's
     `title`; the id is the LEAD, gated at `FEED_NO_ID`; result keeps a 130px `meta` slot.
     SUBMITTED AND COMPLETED ARE ONE `AGE_SLOT`: two 158px timestamp columns cost 316px, and dispatch here
     is synchronous so `Completed` is an em dash on three rows in four. The slot shows the COMPACT age of
     the submission with BOTH instants on its `title`. It says `20m`, NOT `20m ago`: at the cluster's 11px
     mono (~6.6px a character) the local 44px held 6.6 characters against the 7-8 every verbose
     `relativeAge` value from `10s ago` up needs, so it clipped every row at every width; `compactAge` is
     bounded at four characters (`364d`), what `AGE_SLOT`'s 28 was measured against.
     THE ROW OPENS ITS COMMAND (`CommandDetailPage.tsx`): the args and result JSON are a detail page's, not a fold. */
  const commandRows: FeedRowSpec[] = items.map((cmd): FeedRowSpec => {
    const token = statusToken(cmd.status)
    const isOffline = nodeStatus[cmd.node_id]?.status === 'offline'
    const path = commandPath(cmd.id)
    const target = targetLabel(cmd)
    /* A ROW BOUND TO NO NODE PRINTS THE PICKER'S OWN WORDS, not an empty slot. `""` is how the wire
       spells the NULL reference (`ON DELETE SET NULL`) and `shortNode('')` is `''`, so these rows used
       to render a blank Node cell — survivable scattered one per hundred pages, but the `No node` option
       makes them a whole screenful and fifty blank slots read as a rendering fault. */
    const nodeName = cmd.node_id ? shortNode(cmd.node_id) : UNBOUND_NODE_LABEL

    return {
      key: String(cmd.id),
      indicator: {
        tone: toneFor(`badge-${token}`),
        glyph: STATUS_GLYPH[token],
        label: token,
        // The token an oracle reads, moved onto the element that now carries the state.
        data: { status: token },
      },
      lead: { text: `#${cmd.id}`, width: 46, title: `Command ${cmd.id}` },
      title: `${cmd.command_type} · ${target}`,
      titleText: `#${cmd.id} ${cmd.command_type} · ${target}`,
      /* NAMED, not only washed: `muted` is colour, and colour alone is not a signal every reader gets.
         The row's own sentence says the node is offline, so a screen reader hears the fact the wash
         shows — and hears it at the widths where the Node slot's chip is gated away. */
      ariaLabel:
        `Command ${cmd.id}: ${cmd.command_type} on ${nodeName}${isOffline ? ' (offline)' : ''} — ${token}`,
      /* THE WHOLE-ROW WASH the pre-cutover `opacity: 0.55` was. Ungated by construction, so it is the
         half of the offline signal that survives the phone (see the Node slot's chip). */
      muted: isOffline,
      data: { 'command-id': cmd.id },
      /* The title links to the command; the row body opens the same page. */
      href: path,
      current: isOpenPath(openPath, path),
      onRowClick: () => navigate(path),
      rowClickLabel: `Open command ${cmd.id}`,
      slots: [
        {
          key: 'result',
          width: 130,
          gate: 'meta',
          testId: 'col-cmd-result',
          title: resultSummary(cmd),
          node: resultSummary(cmd),
        },
        {
          key: 'node',
          width: 104,
          gate: 'id',
          mono: true,
          testId: 'col-cmd-node',
          title: cmd.node_id
            ? `${cmd.node_id}${isOffline ? ' — this node is offline' : ''}`
            : UNBOUND_NODE_LABEL,
          node: (
            <>
              {nodeName}
              {/* THE CHIP NAMES IT, THE ROW'S `muted` STILL WASHES IT. The chip rides the node it is
                  about, which is where the fact belongs — but this slot is `gate: 'id'` and gone below
                  768px, so the chip alone would leave a phone with no offline signal at all. `muted` is
                  the feed's per-row wash and survives every width, which is the pre-cutover whole-row
                  opacity this replaced, so both are set rather than either. */}
              {isOffline && (
                <StatusChip label="OFFLINE" tone={toneFor('badge-node-offline')} data-status="node-offline" />
              )}
            </>
          ),
        },
        {
          key: 'when',
          width: AGE_SLOT,
          mono: true,
          dim: true,
          testId: 'col-cmd-when',
          title: `Submitted ${absoluteTime(cmd.created_at)} · Completed ${absoluteTime(cmd.completed_at)}`,
          node: compactAge(cmd.created_at),
        },
      ],
    }
  })

  return (
    /* THE TOOLBAR: every control is a QUERY PARAMETER. A Node picker over the fifty loaded rows rendered 2
       of a node's 239 with nothing saying the answer was partial, and could not select a node absent from
       the page. The frame is the one the audit tabs share (`PagedLogView`): two tabs of one page, one search box.
       IT IS ALWAYS RENDERED: hiding it on a page whose rows name one node would hide what finds the rest. */
    <PagedLogView
      testIdPrefix="nodes"
      search={{
        fields: searchFields,
        ariaLabel: 'Search commands',
        /* IT NAMES WHAT IS SEARCHED, NOT WHAT IS DISPLAYED, FOR EVERY READER. The predicates run over the
           command type and the raw `args`/`result` JSON, which the Target/Result columns render lossily
           (`{"project":"acme","number":19}` shows as `acme #19`), so "target" would invite a string the
           document does not contain. And for a TENANT the server matches the type alone
           (`routes/portfolio.py::_command_log_filtered` drops the payload predicates for a reader whose
           payload it narrows, or the search would be an oracle for what it withholds) — so the promise is
           the word that matches for everyone, smaller than the operator's truth, never larger. */
        placeholder: 'type…',
      }}
      filters={
        <>
          <FilterSelect
            label="Node"
            ariaLabel="Filter by node"
            value={nodeFilter}
            onChange={next => narrow(() => setNodeFilter(next))}
            allLabel="All nodes"
            options={nodeOptions}
            minWidth={160}
          />
          <FilterSelect
            label="Status"
            ariaLabel="Filter by status"
            value={status}
            onChange={next => narrow(() => setStatus(next))}
            allLabel="Any status"
            options={STATUS_OPTIONS}
            minWidth={150}
          />
        </>
      }
      range={rangeFields}
      zone={zone}
      hasFilters={hasFilters}
      onClear={clearFilters}
    >
      {error && <Banner severity="error">{error}</Banner>}

      {/* THE EMPTY DISCRIMINATOR IS THE FILTERS THIS PAGE SET, not a second whole-table count off the wire —
          the page already knows whether it narrowed anything, so `GET /api/command-log` is not asked for
          a second whole-table count. `ariaLabel` and `command-log-table`
          are kept from the `<Table>` era — the latter is what `e2e/audit-tabs.spec.ts` scopes every
          Nodes-tab row oracle to. */}
      <ListStates
        loading={loading}
        skeleton={{ rows: 12 }}
        empty={items.length === 0}
        noMatch={hasFilters && <NoMatchesState title="No matching commands" />}
        emptyTitle="No commands yet"
        feed={{ ariaLabel: 'Command log', testId: 'command-log-table', sections: [{ key: 'commands', rows: commandRows }] }}
      />

      {/* THE PAGER IS DERIVED FROM `total`, WHICH IS WHY IT CAN BE HONEST. The condition it replaces
          was `commands.length === per` and the Next button had no `disabled` at all — with no count on
          the wire there was nothing to compare against, so Next stayed live past the last page and
          "Page 3" was the whole readout: 388 pages of blind paging over today's 19,385 rows, and no way
          to tell a filtered page from the end of the log. Two other pages in this repo were built not
          to repeat that, and named this endpoint as the example until this change
          (`pages/credentials/api.ts`, `pages/credentials/Credentials.tsx`, both since updated). */}
      <PageNav
        testId="nodes-pagination"
        page={page}
        setPage={setPage}
        totalPages={totalPages}
        sx={{ mt: 1.5, pt: 1, borderTop: 1, borderColor: 'divider' }}
      />
    </PagedLogView>
  )
}
