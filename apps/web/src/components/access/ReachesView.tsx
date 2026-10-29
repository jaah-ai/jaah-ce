import { useMemo, useState, type JSX, type ReactNode } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { useAuth } from '../../auth'
import { ORG_ACCESS_URL } from '../../lib/orgAdmin'
import { useApiRead } from '../../hooks/useApiRead'
import { absoluteTime, shortDay } from '../../lib/relativeTime'
import { Banner } from '../../ui/Banner'
import { EmptyState } from '../../ui/EmptyState'
import { groupLabel } from '../../types'
import { tapTargetSx } from '../../ui/listStyles'
import { LoadingState } from '../../ui/LoadingState'
import SectionCard from '../../ui/SectionCard'
import AddAccessDialog from './AddAccessDialog'
import { useScopeOptions } from './ScopePicker'
import { getReaches, listRoles, type ReachBinding, type ReachScope } from './api'
import { useCanManage, useRemoveBinding } from './useBindings'

// A user's Access: every live binding they hold, directly or through a group, grouped org → project,
// plus accounts (`GET /api/access/reaches/{user_id}`). Inheritance is drawn, not listed: an org binding
// applies to every project, so each project ends on ONE muted line naming the org roles it inherits, never a line
// per binding, so the project's own roles stand out (how each org role is held reads under Organization).
// A viewer who may manage the user's org adds here (the Add dialog with this user fixed as the principal,
// as a group's Bindings tab) and removes a DIRECT binding; a group's or an inherited line is changed where
// it is held.

type Source = { text: string; href?: string; kind: 'direct' | 'group' }

const LINE_SX = { display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', columnGap: 1, rowGap: 0.25, py: 0.5, minWidth: 0 } as const

/** A group as this viewer reads it (`types.ts::groupLabel`), so a "Via group" line and the Member-of chip agree. */
type GroupName = (storedName: string | null) => string

function sourceOf(b: ReachBinding, groupName: GroupName): Source {
  if (b.source.kind === 'group') {
    return {
      kind: 'group',
      text: `Via group ${groupName(b.source.via_name)}`,
      href: b.source.via_id ? `/groups/${b.source.via_id}` : undefined,
    }
  }
  return { kind: 'direct', text: 'Direct' }
}

/** A project's one line for every org role it inherits: each role named once, however many bindings grant it. */
function InheritedLine({ bindings }: { bindings: ReachBinding[] }) {
  if (bindings.length === 0) return null
  const roles = [...new Set(bindings.map(b => b.role.name))].join(', ')
  return (
    <Box
      component="li"
      data-testid="reach-binding"
      data-source="inherited"
      sx={LINE_SX}
    >
      <Typography variant="body2" component="span" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>{roles}</Typography>
      <Typography variant="body2" component="span" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
        Inherited from {bindings[0].scope.name}
      </Typography>
    </Box>
  )
}

function BindingLine({ binding, source, onRemove }: { binding: ReachBinding; source: Source; onRemove?: (b: ReachBinding) => void }) {
  return (
    <Box
      component="li"
      data-testid="reach-binding"
      data-source={source.kind}
      sx={{ display: 'flex', alignItems: 'center', columnGap: 1, minWidth: 0 }}
    >
      {/* The words wrap inside their own box; Remove never shrinks, so it stays on the line it removes. */}
      <Box sx={{ ...LINE_SX, flex: 1 }}>
        <Typography variant="body2" component="span" sx={{ fontWeight: 600 }}>{binding.role.name}</Typography>
        <Typography variant="body2" component="span" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
          {source.href ? <Link component={RouterLink} to={source.href}>{source.text}</Link> : source.text}
        </Typography>
        {binding.expires_at && (
          <Typography variant="caption" component="span" sx={{ color: 'text.secondary' }} title={absoluteTime(binding.expires_at)}>
            Expires {shortDay(binding.expires_at)}
          </Typography>
        )}
      </Box>
      {/* Only a direct line: the binding is this user's own, so removing it here removes what the line says. */}
      {onRemove && source.kind === 'direct' && (
        <Button
          size="small"
          color="error"
          aria-label={`Remove ${binding.role.name} on ${binding.scope.name}`}
          onClick={() => onRemove(binding)}
          sx={[tapTargetSx, { flexShrink: 0 }]}
        >
          Remove
        </Button>
      )}
    </Box>
  )
}

function Lines({ children }: { children: ReactNode }) {
  return <Box component="ul" sx={{ listStyle: 'none', m: 0, p: 0 }}>{children}</Box>
}

function ScopeHeading({ label, name, href }: { label: string; name: string; href?: string }) {
  return (
    <Typography variant="body2" component="h4" sx={{ m: 0, fontWeight: 600, overflowWrap: 'anywhere' }}>
      <Typography component="span" variant="caption" sx={{ color: 'text.secondary', mr: 0.75, textTransform: 'uppercase', letterSpacing: 0.4 }}>
        {label}
      </Typography>
      {href ? <Link component={RouterLink} to={href}>{name}</Link> : name}
    </Typography>
  )
}

function ScopeBlock({ testId, heading, children, indent = 0 }: { testId: string; heading: ReactNode; children: ReactNode; indent?: number }) {
  return (
    <Box data-testid={testId} sx={{ pl: indent, borderLeft: indent ? 2 : 0, borderColor: 'divider', ml: indent ? 0.5 : 0 }}>
      {heading}
      {children}
    </Box>
  )
}

/** The Add button and its dialog, mounted only for a viewer who may manage `orgId`: the scope and role
 *  reads are the dialog's, not the list's. Scopes are the org's own, as a group's Bindings tab offers. */
function AddUserAccess({ userId, userName, orgId, onAdded }: {
  userId: string
  userName: string
  orgId: string
  onAdded: () => Promise<void>
}): JSX.Element {
  const { options, loading } = useScopeOptions(orgId)
  const roles = useApiRead(['access-roles'], listRoles, 'Could not load roles.')
  const scopes = useMemo(
    () => (loading ? null : options.filter(o => o.orgId === orgId || o.type === 'account')),
    [orgId, loading, options],
  )
  const [adding, setAdding] = useState(false)
  const busy = !scopes || (!roles.data && !roles.error)
  return (
    <>
      {roles.error && <Banner severity="error">{roles.error}</Banner>}
      <Box>
        {/* Busy, not a silent grey box, while the dialog's two reads land; a failed roles read stays plain
            disabled under its banner. */}
        <Button
          variant="contained"
          data-testid="access-add"
          disabled={!roles.data || !scopes}
          aria-busy={busy}
          onClick={() => setAdding(true)}
          sx={tapTargetSx}
        >
          {busy ? 'Loading…' : 'Add'}
        </Button>
      </Box>
      {adding && scopes && (
        <AddAccessDialog
          principal={{ kind: 'user', id: userId, name: userName }}
          roles={roles.data ?? []}
          scopeOptions={scopes}
          scopeLoading={false}
          onClose={() => setAdding(false)}
          onAdded={onAdded}
        />
      )}
    </>
  )
}

export default function ReachesView({ userId, userName, orgId: userOrgId = null, groups = [] }: {
  userId: string
  /** The name the Add dialog and the Remove confirm show. */
  userName: string
  /** The user's org (`UserDetail.org_id`); absent → the org their own org binding names, else the viewer's. */
  orgId?: string | null
  /** The user's groups (`Membership.group` + `is_everyone`), for the Member-of chips and the "Via group" lines. */
  groups?: { name: string; is_everyone?: boolean }[]
}): JSX.Element {
  const { admin } = useAuth()
  const operator = admin?.is_operator === true
  /* By exact stored name: names are unique across every org, so the match is the user's own Everyone group,
     never a hand-made one that merely reads like it. */
  const groupName: GroupName = stored => {
    if (!stored) return 'group'
    return groupLabel(groups.find(g => g.name === stored) ?? { name: stored }, operator)
  }
  const reaches = useApiRead(['reaches-view', userId], () => getReaches(userId), 'Could not load what this user reaches.')
  const data = reaches.data
  // Until the reaches land, an absent org may still be named by them: no viewer fallback yet.
  const orgId = userOrgId ?? (data ? data.org[0]?.scope.id ?? admin?.org?.id ?? null : null)
  // The same probe as a scope's Access tab: may the viewer manage the org's bindings.
  const canManage = useCanManage(orgId ? { type: 'org', id: orgId } : null)
  const { remove: removeAccess, error, clearError, confirmDialog } = useRemoveBinding(reaches.reload)
  const remove = (b: ReachBinding) =>
    removeAccess(b.binding_id, `Remove ${b.role.name} on ${b.scope.name}? ${userName} loses what it grants.`)

  if (reaches.error) return <Banner severity="error">{reaches.error}</Banner>
  if (!data) return <LoadingState rows={4} />

  const onRemove = canManage ? (b: ReachBinding) => void remove(b) : undefined
  // An operator viewing an account whose org nothing names: the Add has no org to scope to.
  const manage = canManage && orgId ? (
    <AddUserAccess userId={userId} userName={userName} orgId={orgId} onAdded={reaches.reload} />
  ) : !orgId && admin?.is_operator === true ? (
    <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap', rowGap: 0.5 }}>
      <Button variant="contained" data-testid="access-add" disabled sx={tapTargetSx}>Add</Button>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>No organization known for this user.</Typography>
    </Stack>
  ) : null
  const errorBanner = error ? <Banner severity="error" onDismiss={clearError}>{error}</Banner> : null

  const empty = !data.org.length && !data.projects.length && !data.accounts.length
  const orgName = data.org[0]?.scope.name
  const orgScopeId = data.org[0]?.scope.id

  const groupLine = groups.length > 0 && (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, alignItems: 'center' }} data-testid="reach-groups">
      <Typography variant="body2" sx={{ color: 'text.secondary' }} component="span">Member of</Typography>
      {groups.map(g => <Chip key={g.name} size="small" variant="outlined" label={groupLabel(g, operator)} />)}
    </Box>
  )

  if (empty) {
    return (
      <Stack spacing={1}>
        {manage}
        {errorBanner}
        {groupLine}
        <EmptyState title={canManage ? 'No access yet.' : 'No access'} />
        {confirmDialog}
      </Stack>
    )
  }

  const projectBlock = (p: ReachScope) => (
    <ScopeBlock
      key={p.id}
      testId="reach-project"
      indent={orgName ? 1.5 : 0}
      heading={<ScopeHeading label="Project" name={p.name} href={`/projects/${p.id}?tab=access`} />}
    >
      <Lines>
        {p.bindings.map(b => <BindingLine key={b.binding_id} binding={b} source={sourceOf(b, groupName)} onRemove={onRemove} />)}
        <InheritedLine bindings={data.org} />
      </Lines>
    </ScopeBlock>
  )

  return (
    <Stack spacing={1.5} data-testid="reaches">
      {manage}
      {errorBanner}
      {groupLine}
      {(data.org.length > 0 || data.projects.length > 0) && (
        <SectionCard title="Organization and projects">
          <Stack spacing={1.5}>
            {orgName && (
              <ScopeBlock
                testId="reach-org"
                heading={<ScopeHeading label="Organization" name={orgName} href={orgScopeId ? (admin?.is_operator === true ? `/customers/${encodeURIComponent(orgScopeId)}?tab=access` : ORG_ACCESS_URL) : undefined} />}
              >
                <Lines>
                  {data.org.map(b => <BindingLine key={b.binding_id} binding={b} source={sourceOf(b, groupName)} onRemove={onRemove} />)}
                </Lines>
              </ScopeBlock>
            )}
            {data.projects.map(projectBlock)}
          </Stack>
        </SectionCard>
      )}
      {data.accounts.length > 0 && (
        <SectionCard title="Accounts">
          <Stack spacing={1.5}>
            {data.accounts.map(a => (
              <ScopeBlock key={a.id} testId="reach-account" heading={<ScopeHeading label="Account" name={a.name} href={`/accounts/${a.id}?tab=access`} />}>
                <Lines>
                  {a.bindings.map(b => <BindingLine key={b.binding_id} binding={b} source={sourceOf(b, groupName)} onRemove={onRemove} />)}
                </Lines>
              </ScopeBlock>
            ))}
          </Stack>
        </SectionCard>
      )}
      {confirmDialog}
    </Stack>
  )
}
