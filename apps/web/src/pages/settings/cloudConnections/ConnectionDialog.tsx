import { type JSX, useEffect, useRef, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import FormControl from '@mui/material/FormControl'
import FormControlLabel from '@mui/material/FormControlLabel'
import FormLabel from '@mui/material/FormLabel'
import Link from '@mui/material/Link'
import MenuItem from '@mui/material/MenuItem'
import Radio from '@mui/material/Radio'
import RadioGroup from '@mui/material/RadioGroup'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../../api'
import { useAuth } from '../../../auth'
import { useConfirm } from '../../../components/ConfirmDialog'
import { useSubmit } from '../../../hooks/useSubmit'
import AdvancedSection from '../../../ui/AdvancedSection'
import Banner from '../../../ui/Banner'
import { credentialMark } from '../../../ui/credentialMarks'
import FormDialog from '../../../ui/FormDialog'
import FormField from '../../../ui/FormField'
import FormTextField from '../../../ui/FormTextField'
import HelpLabel from '../../../ui/HelpLabel'
import { fieldTapTargetSx, ID_SX } from '../../../ui/listStyles'
import type {
  CloudConnectionCredentialOption, CloudConnectionRow, CloudProvider, CloudProviderSpec, ConnectionField,
} from '../../../types'
import { deleteCredential } from '../../credentials/api'
import { NODELINK_STORE_URL } from '../../nodes/nodeLinkStore/api'
import {
  createCloudConnection,
  DEFAULT_REGION,
  initialRegion,
  listCloudConnectionOptions,
  listCloudProviders,
  listRegions,
  listSecurityGroups,
  listSubnets,
  lookupConnectionField,
  PROVIDER_LABELS,
  updateCloudConnection,
  type CloudConnectionBody,
  type LookupOption,
  type RegionOption,
  type SecurityGroupOption,
  type SubnetOption,
} from './api'
import { ConnectionImageText, connectionImage } from './connectionImage'
import AwsCredentialFields, { type AwsCredentialHandle } from './AwsCredentialFields'

/* The ONE Add/Edit connection dialog: Networks > Connections opens it on a row, and the
 * Add-node wizard opens it from its Connection select's "Add new…". It fetches its own options (the
 * credentials in reach, the region catalogue), so a caller passes only the row — and, from the wizard, the
 * org the node is ordered for (`orgId`): the operator's roster carries every org's credentials, and a
 * connection must be built on the ordering org's own.
 * First view = what a working connection needs (provider, name, credential, region); everything optional folds
 * into Advanced, most common first. Subnet and security group are "Auto" by default and RESOLVED by the
 * route on save (`cloud_connection_network.resolve_network`), so the saved row shows real ids.
 * THE PROVIDERS ARE THE SERVER'S (`GET …/providers`, only those on the caller's plan): AWS keeps its own form;
 * any other cloud renders ONE generic form from its `fields` (`CloudProviderSpec`), no per-cloud component.
 * NodeLink is not set here: one line links the org's NodeLinks (`pages/nodes/nodeLinkStore`), for the operator or
 * when the caller's org has NodeLink (`Admin.nodelink_enabled`). */

/** The Credential select's last item: swaps in `AwsCredentialFields`. Never a credential id. */
const NEW_CREDENTIAL = '__new__'

/** A credential's type; a server that predates `type` offered AWS credentials only. */
const credentialType = (c: CloudConnectionCredentialOption): string => c.type || 'aws'

/** The server's default (`cloud_node_defaults.root_volume_gb` server_default 50), as a string: parsing
 *  on input would turn a half-typed "4" into 4 GB, so the volume is parsed at submit. */
const DEFAULT_ROOT_VOLUME_GB = '50'

type Draft = {
  name: string
  description: string
  provider: CloudProvider
  credential_id: string
  region: string
  ami_id: string
  /** In order: the first is the connection's `subnet_id`, the rest its `extra_subnet_ids`; `[]` = Auto. */
  subnet_ids: string[]
  security_group_id: string
  root_volume_gb: string
  /** A non-AWS cloud's network, by `ConnectionField.name`: `''`/`[]` = Auto, resolved on save. */
  network: Record<string, string | string[]>
}

type DraftField = keyof Draft

/** AWS error codes that refuse the CREDENTIAL itself (bad, expired or unauthorised keys; an account not
 *  opted in), not an id the operator typed — each with the plain sentence that leads AWS's own text, so a
 *  reader need not know EC2 action names. */
const KEYS_REJECTED = 'AWS rejected this credential’s keys.'
const CREDENTIAL_REFUSALS: Record<string, string> = {
  AuthFailure: KEYS_REJECTED,
  InvalidClientTokenId: KEYS_REJECTED,
  SignatureDoesNotMatch: KEYS_REJECTED,
  IncompleteSignature: KEYS_REJECTED,
  MissingAuthenticationToken: 'AWS received no keys for this credential.',
  ExpiredToken: 'This credential’s session token has expired.',
  RequestExpired: 'AWS rejected the request’s timestamp; the control plane’s clock may be off.',
  UnauthorizedOperation: 'AWS denied this credential a permission the connection needs.',
  OptInRequired: 'This AWS account is not enabled for this region or service.',
  Blocked: 'AWS has blocked this account.',
  PendingVerification: 'AWS is still verifying this account.',
}

/** The AWS code of a refusal in the route's wording (`cloud_connection_network.aws_errors`), if any. */
function awsCode(detail: string): string | undefined {
  return /^AWS refused \w+: (\w+)/.exec(detail)?.[1]
}

/** A save refusal that is the credential's, so it renders beside the Credential field — where the lookup's
 *  warning about the same cause sits — instead of as a second copy at the top. Anything else (a typed
 *  subnet AWS does not know, AWS unreachable) stays the form's. */
function credentialRefusal(detail: string): 'credential_id' | null {
  if (/^credential '.*' cannot sign an AWS request/.test(detail)) return 'credential_id'
  const code = awsCode(detail)
  return code && Object.hasOwn(CREDENTIAL_REFUSALS, code) ? 'credential_id' : null
}

/** A refusal as the operator reads it, under the Credential or in the lookup's warning alike, closed — AWS's
 *  sentence may end bare. A credential refusal is ONE plain sentence; AWS's own text (code and message) is
 *  `aws`, shown as a secondary line (`AwsCode`). Any other refusal is AWS's text itself. The EC2 action is
 *  dropped: the lookup and the save fail on different ones for the same keys, and neither tells the operator
 *  what to fix. */
function plainRefusal(detail: string): { sentence: string; code?: string; aws?: string } {
  const text = detail.trim()
  const closed = /[.!?]$/.test(text) ? text : `${text}.`
  const code = awsCode(text)
  if (code === undefined) return { sentence: closed }
  const aws = closed.replace(/^AWS refused \w+: /, '')
  return Object.hasOwn(CREDENTIAL_REFUSALS, code) ? { sentence: CREDENTIAL_REFUSALS[code], code, aws } : { sentence: `AWS refused: ${aws}` }
}

/** AWS's code under a plain refusal; AWS's whole sentence on hover or tap. */
function AwsCode({ code, aws }: { code: string; aws: string }): JSX.Element {
  return (
    <Typography variant="caption" component="span" data-testid="cloud-connection-aws-code" aria-description={aws} sx={{ color: 'text.secondary' }}>
      AWS: <HelpLabel label={code} help={aws} />
    </Typography>
  )
}

function draftFrom(row: CloudConnectionRow | null): Draft {
  return {
    name: row?.name ?? '',
    description: row?.description ?? '',
    provider: row?.provider ?? 'aws',
    credential_id: row?.credential_id ?? '',
    region: row?.region ?? '',
    /* `''` = the newest Ubuntu 24.04 LTS image, resolved at every launch (the route's contract). */
    ami_id: row?.ami_id ?? '',
    /* `[]` = Auto. A saved row always carries resolved ids; the select offers them even before the
       lookup answers (see `withCurrent`). */
    subnet_ids: row ? [row.subnet_id, ...(row.extra_subnet_ids ?? [])].filter(id => id !== '') : [],
    security_group_id: row?.security_group_id ?? '',
    root_volume_gb: row ? String(row.root_volume_gb) : DEFAULT_ROOT_VOLUME_GB,
    /* A saved row's RESOLVED network, so a blank saved as Auto reopens on what it became. */
    network: row && row.provider !== 'aws' ? networkFrom(row.network) : {},
  }
}

function networkFrom(network: Record<string, unknown> | undefined): Record<string, string | string[]> {
  const out: Record<string, string | string[]> = {}
  for (const [name, value] of Object.entries(network ?? {})) {
    out[name] = Array.isArray(value) ? value.map(String) : value == null ? '' : String(value)
  }
  return out
}

/** Where the Region opens for a credential: AWS's own rule (`initialRegion`); another cloud's credential
 *  region, else none until picked. */
function regionOf(provider: CloudProvider, credential: CloudConnectionCredentialOption | undefined): string {
  return provider === 'aws' ? initialRegion(credential) : credential?.region || ''
}

/* The VPC is in the label: once one subnet is picked, the other VPCs' are greyed (one connection, one VPC). */
function subnetLabel(subnet: SubnetOption): string {
  const base = `${subnet.id} · ${subnet.availability_zone} · ${subnet.cidr_block} · ${subnet.vpc_id}`
  return subnet.name ? `${base} (${subnet.name})` : base
}

/** The options list with the draft's current id in it: a saved row's id is a fact even when the lookup
 *  has not answered (or refused), and MUI renders an unlisted select value blank. */
function withCurrent<T extends { id: string }>(items: T[], current: string, make: (id: string) => T): T[] {
  if (current === '' || items.some(item => item.id === current)) return items
  return [make(current), ...items]
}

export type ConnectionDialogProps = {
  /** The row being edited, or `null` to add one. */
  row: CloudConnectionRow | null
  onClose: () => void
  onSaved: (saved: CloudConnectionRow) => void | Promise<void>
  /** The org the connection is for: only ITS credentials are offered, and a credential added inline is
   *  filed there. Absent (Settings): every credential in reach. */
  orgId?: string
  /** That org's name, for the inline credential form's note. */
  orgName?: string
}

export default function ConnectionDialog({ row, onClose, onSaved, orgId, orgName }: ConnectionDialogProps): JSX.Element {
  const { admin } = useAuth()
  const isOperator = admin?.is_operator === true
  /* Opened for an org that is not the caller's own (an operator ordering a node for a tenant). */
  const foreignOrg = orgId !== undefined && orgId !== (admin?.org?.id ?? '')
  /* The NodeLinks line's audience: the operator, or anyone whose org has NodeLink; never for another org's dialog
     (the link opens the CALLER'S org's list). */
  const showNodeLink = (isOperator || admin?.nodelink_enabled === true) && !foreignOrg

  const [credentials, setCredentials] = useState<CloudConnectionCredentialOption[]>([])
  /* `null` until the server answers: no radio is drawn for a cloud the plan does not offer. */
  const [providers, setProviders] = useState<CloudProviderSpec[] | null>(null)
  const [regions, setRegions] = useState<RegionOption[]>([])
  const [optionsError, setOptionsError] = useState('')
  /* What a blank image launches, named by the server (`ec2.UBUNTU_RELEASE`); a saved row carries it too. */
  const [autoImage, setAutoImage] = useState(row?.auto_image ?? '')
  const [draft, setDraft] = useState<Draft>(() => draftFrom(row))
  /* The Region follows the credential (its own `region`, else `us-east-1`) UNTIL the operator picks one;
     after that a credential change leaves it alone. */
  const regionChosen = useRef(row !== null)
  /* The Credential slot: the select, or the inline form for a NEW credential ("Add new…") or for the picked
     one's keys ("Edit credential", offered once AWS refused them). */
  const [credentialMode, setCredentialMode] = useState<'pick' | 'new' | 'edit'>('pick')
  const credentialForm = useRef<AwsCredentialHandle>(null)
  /* A credential "Add new…" filed in THIS dialog, until the operator opens it: if AWS then refuses it, the
     dialog says once that it was saved, so a bad credential does not linger in the roster unnoticed. */
  const [filedInline, setFiledInline] = useState('')
  const [confirm, confirmDialog] = useConfirm()

  const { busy, errors, setErrors, formError, run } = useSubmit<DraftField>(credentialRefusal)
  /* THE REFUSAL DIES WITH ITS CAUSE — only this field's error; `errors` is otherwise rebuilt at submit. */
  const clearError = (field: DraftField) => setErrors(prev => {
    if (prev[field] === undefined) return prev
    const next = { ...prev }
    delete next[field]
    return next
  })
  const set = <K extends DraftField>(field: K, value: Draft[K]) => {
    setDraft(prev => ({ ...prev, [field]: value }))
    clearError(field)
  }

  /* Options and the region catalogue in one pass; a mount-generation guard drops a late answer. */
  useEffect(() => {
    let live = true
    void (async () => {
      try {
        const [options, catalogue, clouds] = await Promise.all([listCloudConnectionOptions(), listRegions(), listCloudProviders(foreignOrg ? orgId : undefined)])
        if (!live) return
        const offered = (options?.credentials ?? []).filter(c => orgId === undefined || c.org_id === orgId)
        const specs = Array.isArray(clouds) ? clouds : []
        setCredentials(offered)
        setProviders(specs)
        if (options?.auto_image) setAutoImage(options.auto_image)
        setRegions(Array.isArray(catalogue) ? catalogue : [])
        setOptionsError('')
        /* A create opens on AWS, else the plan's first cloud. PRESELECT ITS ONLY CREDENTIAL (the common
           one-account case needs no menu), and open the Region on it. */
        if (row === null) {
          setDraft(prev => {
            if (prev.credential_id !== '') return prev
            const spec = specs.find(p => p.key === prev.provider) ?? specs[0]
            const provider = spec?.key ?? prev.provider
            const matching = offered.filter(c => credentialType(c) === (spec?.credential_type ?? provider))
            const first = matching.length === 1 ? matching[0] : undefined
            return {
              ...prev,
              provider,
              credential_id: first?.id ?? '',
              region: prev.region || regionOf(provider, first),
            }
          })
        }
      } catch (err) {
        if (!live) return
        setOptionsError(apiDetail(err, 'Failed to load the credentials.'))
      }
    })()
    return () => { live = false }
  }, [row, orgId, foreignOrg])

  /* ── Subnet / security group lookups ─────────────────────────────────────────────────────────
     Read from AWS whenever a credential AND a region are set; a stale answer (a quicker earlier request)
     is dropped by generation. A refusal is a WARNING under the Credential (once: the operator cannot tell
     which call failed, and the credential is what it refused), never a field error — Auto still saves, and the launch resolves them itself. */
  /* `null` = not answered (none asked yet, in flight, or refused): only an ANSWER can say there is no
     default VPC. */
  const [subnets, setSubnets] = useState<SubnetOption[] | null>(null)
  const [securityGroups, setSecurityGroups] = useState<SecurityGroupOption[]>([])
  const [lookupError, setLookupError] = useState('')
  const lookupGeneration = useRef(0)
  /* The credential and region AWS last refused the CREDENTIAL on, and its refusal: asked again only once either
     changes or the keys are saved anew — not on every swap back from "Edit credential". */
  const refusedLookup = useRef<{ key: string; error: string } | null>(null)
  const lookupCredential = credentialMode === 'pick' ? draft.credential_id : ''
  const isAws = draft.provider === 'aws'
  useEffect(() => {
    const mine = ++lookupGeneration.current
    setSubnets(null)
    setSecurityGroups([])
    setLookupError('')
    if (!isAws || lookupCredential === '' || draft.region === '') return
    const key = `${lookupCredential} ${draft.region}`
    if (refusedLookup.current?.key === key) {
      setLookupError(refusedLookup.current.error)
      return
    }
    refusedLookup.current = null
    void (async () => {
      try {
        const [nextSubnets, nextGroups] = await Promise.all([
          listSubnets(lookupCredential, draft.region),
          listSecurityGroups(lookupCredential, draft.region),
        ])
        if (mine !== lookupGeneration.current) return
        setSubnets(Array.isArray(nextSubnets) ? nextSubnets : [])
        setSecurityGroups(Array.isArray(nextGroups) ? nextGroups : [])
      } catch (err) {
        if (mine !== lookupGeneration.current) return
        /* Raw: rendered through `plainRefusal`, and read by `credentialRefusal` for the Edit link. */
        const error = apiDetail(err, 'AWS could not be reached.')
        if (credentialRefusal(error) !== null) refusedLookup.current = { key, error }
        setLookupError(error)
      }
    })()
    return () => { lookupGeneration.current += 1 }
  }, [isAws, lookupCredential, draft.region])

  /* ── Any other cloud's lookups (`lookup/{field}`) ────────────────────────────────────────────────
     The regions once a credential is picked; each looked-up field once the region is too, again whenever a
     `parent` moves. A refusal is the same WARNING as AWS's, never a field error: Auto still saves. */
  const spec = providers?.find(p => p.key === draft.provider)
  const genericFields = isAws ? [] : spec?.fields ?? []
  const [cloudRegions, setCloudRegions] = useState<LookupOption[]>([])
  const [fieldOptions, setFieldOptions] = useState<Record<string, LookupOption[]>>({})
  const [fieldLookupError, setFieldLookupError] = useState('')
  // The region list's own refusal: a field lookup re-run must not wipe it while the credential is still refused.
  const [regionLookupError, setRegionLookupError] = useState('')
  useEffect(() => {
    let live = true
    setCloudRegions([])
    setRegionLookupError('')
    if (isAws || lookupCredential === '') return
    lookupConnectionField('region', draft.provider, lookupCredential).then(
      found => { if (live) setCloudRegions(Array.isArray(found) ? found : []) },
      err => { if (live) setRegionLookupError(apiDetail(err, 'The cloud could not be reached.')) },
    )
    return () => { live = false }
  }, [isAws, draft.provider, lookupCredential])
  /* The values a lookup is narrowed by, as one string, so the effect re-runs on a move and not on every render: each
     field's `parent`, and every typed field (a GCP project), which the server reads as the lookup's context. */
  const contextNames = genericFields.flatMap(f => (f.parent ? [f.parent] : f.lookup ? [] : [f.name]))
  const parentKey = JSON.stringify(contextNames.map(name => [name, draft.network[name] ?? '']))
  useEffect(() => {
    let live = true
    setFieldOptions({})
    setFieldLookupError('')
    const looked = (isAws ? [] : spec?.fields ?? []).filter(f => f.lookup)
    if (looked.length === 0 || lookupCredential === '' || draft.region === '') return
    const context = Object.fromEntries((JSON.parse(parentKey) as [string, unknown][]).map(([name, value]) => [name, String(value ?? '').trim()]))
    /* A field whose parent is Auto has nothing to list yet (Azure's subnet of a vnet Jaah creates): no options, no
       refusal. Its parent's pick lists it. */
    const asked = looked.filter(f => !f.parent || context[f.parent] !== '')
    const typed = Object.fromEntries(genericFields.filter(f => !f.lookup && !f.parent).map(f => [f.name, context[f.name] ?? '']))
    void Promise.allSettled(asked.map(f => lookupConnectionField(
      f.lookup!, draft.provider, lookupCredential, draft.region,
      { ...typed, ...(f.parent ? { [f.parent]: context[f.parent] } : {}) },
    ))).then(results => {
      if (!live) return
      const next: Record<string, LookupOption[]> = {}
      let refusal = ''
      results.forEach((result, i) => {
        if (result.status === 'fulfilled') next[asked[i].name] = Array.isArray(result.value) ? result.value : []
        else refusal ||= apiDetail(result.reason, 'The cloud could not be reached.')
      })
      setFieldOptions(next)
      setFieldLookupError(refusal)
    })
    return () => { live = false }
  }, [isAws, spec, draft.provider, lookupCredential, draft.region, parentKey])

  /* A subnet and a security group belong to ONE account and region: whenever either moves, the picked ids
     go back to Auto (they could not exist on the other side; the lookup re-lists for the new pair). */
  function resetNetwork() {
    set('subnet_ids', [])
    set('security_group_id', '')
    // Another cloud's looked-up values are the account's and region's alike; a typed one (a project) stays.
    setDraft(prev => ({
      ...prev,
      network: Object.fromEntries(Object.entries(prev.network).filter(([name]) => !genericFields.some(f => f.name === name && f.lookup))),
    }))
  }

  /* A field and everything it narrows: a child's value is its parent's, so a parent move clears it. */
  function setNetwork(name: string, value: string | string[]) {
    setDraft(prev => {
      const network = { ...prev.network, [name]: value }
      const cleared = new Set([name])
      for (const f of genericFields) {
        if (f.parent && cleared.has(f.parent)) {
          network[f.name] = f.kind === 'multiselect' ? [] : ''
          cleared.add(f.name)
        }
      }
      return { ...prev, network }
    })
  }

  /* A cloud is its own credentials, regions and network: all start over (the only matching credential picked). */
  function chooseProvider(key: CloudProvider) {
    const next = providers?.find(p => p.key === key)
    const matching = credentials.filter(c => credentialType(c) === (next?.credential_type ?? key))
    const only = matching.length === 1 ? matching[0] : undefined
    regionChosen.current = false
    refusedLookup.current = null
    setCredentialMode('pick')
    setErrors({})
    setDraft(prev => ({
      ...prev, provider: key, credential_id: only?.id ?? '', region: regionOf(key, only),
      subnet_ids: [], security_group_id: '', network: {},
    }))
  }

  function chooseRegion(value: string) {
    set('region', value)
    /* A credential refusal can be the region's (an account not opted in to it): the lookup re-asks. */
    clearError('credential_id')
    resetNetwork()
  }

  function chooseCredential(value: string) {
    if (value === NEW_CREDENTIAL) {
      setCredentialMode('new')
      set('credential_id', '')
      if (!regionChosen.current) set('region', DEFAULT_REGION)
      resetNetwork()
      return
    }
    set('credential_id', value)
    if (!regionChosen.current) set('region', regionOf(draft.provider, credentials.find(c => c.id === value)))
    resetNetwork()
  }

  function pickExisting() {
    setCredentialMode('pick')
    clearError('credential_id')
  }

  /* The picked credential's keys, in place of the select: Create/Save files the fix, then the connection. */
  function editCredential() {
    setCredentialMode('edit')
    setFiledInline('')
    clearError('credential_id')
  }

  /* "delete it" IN PLACE: the dialog stays open on the typed draft, back on the select with nothing picked. */
  async function deleteFiledCredential() {
    const id = draft.credential_id
    const name = credentials.find(c => c.id === id)?.name || id
    const ok = await confirm({
      title: 'Delete credential',
      // Only an AWS credential is filed inline.
      identity: `AWS · ${name}`,
      message: 'This cannot be undone. Your connection draft stays.',
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (!ok) return
    /* `run` clears `errors` up front; a refused delete puts the credential's refusal back, so the note and its
       links stay beside the credential that is still picked. */
    const refusal = errors
    await run(`Failed to delete ${name}.`, async () => {
      try {
        await deleteCredential(id)
      } catch (err) {
        setErrors(refusal)
        throw err
      }
      setCredentials(prev => prev.filter(c => c.id !== id))
      setFiledInline('')
      set('credential_id', '')
      resetNetwork()
    })
  }

  async function submit() {
    const trimmed: Record<string, string> = {}
    for (const key of ['name', 'description', 'credential_id', 'region', 'ami_id', 'security_group_id'] as const) {
      trimmed[key] = draft[key].trim()
    }
    /* Checked BEFORE the round trip, beside the field. Whether the ids exist is the SERVER's. */
    if (trimmed.name === '') {
      setErrors({ name: 'Name is required.' })
      return
    }
    if (credentialMode === 'pick' && trimmed.credential_id === '') {
      setErrors({ credential_id: 'Pick a credential.' })
      return
    }
    if (trimmed.region === '') {
      setErrors({ region: 'Pick a region.' })
      return
    }
    if (isAws && !autoSubnet && draft.subnet_ids.length === 0) {
      setErrors({ subnet_ids: 'Pick a subnet.' })
      return
    }
    /* BLANK MEANS "take the server default" ON A CREATE ONLY — the route's asymmetry. PUT cannot ask for it
       back (`exclude_unset=True`: an omitted key leaves the old size; the route refuses clearing). */
    const volumeRaw = draft.root_volume_gb.trim()
    const volume = Number(volumeRaw)
    if (volumeRaw === '') {
      if (row) {
        setErrors({ root_volume_gb: 'Root volume is required — a whole number of GB.' })
        return
      }
    } else if (!Number.isInteger(volume) || volume <= 0) {
      setErrors({ root_volume_gb: 'Root volume must be a whole number of GB.' })
      return
    }
    /* THE SERVER'S SENTENCE, WHOLE: refusals name AWS objects, not columns, so nothing to route on. */
    await run(row ? `Failed to update ${row.name}.` : `Failed to add ${trimmed.name}.`, async () => {
      /* THE CREDENTIAL FIRST: a refused credential leaves its form open with the refusal beside the field
         (`save()` resolves null) and files no connection. */
      let credentialId = trimmed.credential_id
      if (credentialMode !== 'pick') {
        const saved = await credentialForm.current?.save()
        if (!saved) return
        credentialId = saved.id
        /* THE CREDENTIAL IS FILED ONCE: from here the dialog holds it as a picked EXISTING one, so a
           refused connection (an AWS refusal on the network lookup, say) retried with Create files a
           second connection attempt, never a second credential. An edit may have renamed it. */
        setCredentials(prev => prev.some(c => c.id === saved.id)
          ? prev.map(c => c.id === saved.id ? { ...c, name: saved.name } : c)
          : [...prev, { id: saved.id, name: saved.name, region: trimmed.region || null, type: 'aws', org_id: saved.org_id ?? orgId ?? '' }])
        if (credentialMode === 'new') setFiledInline(saved.id)
        // New keys: the lookup asks AWS again.
        refusedLookup.current = null
        setCredentialMode('pick')
        setDraft(prev => ({ ...prev, credential_id: saved.id }))
      }
      /* Another cloud sends its own `network` (every field, `''`/`[]` = Auto) and none of AWS's flat ones. */
      /* No spec yet (the providers read is in flight or failed): the draft's network as is — a saved row's resolved
         values — never `{}`, which the route would re-resolve to Auto. */
      const network = !spec ? draft.network : Object.fromEntries(genericFields.map(f => {
        const value = draft.network[f.name] ?? (f.kind === 'multiselect' ? [] : '')
        if (Array.isArray(value)) return [f.name, value]
        const text = value.trim()
        return [f.name, f.kind === 'int' && text !== '' && Number.isFinite(Number(text)) ? Number(text) : text]
      }))
      const body: CloudConnectionBody = !isAws ? {
        name: trimmed.name,
        description: trimmed.description,
        provider: draft.provider,
        credential_id: credentialId,
        region: trimmed.region,
        ami_id: trimmed.ami_id,
        network,
        root_volume_gb: volumeRaw === '' ? undefined : volume,
      } : {
        name: trimmed.name,
        description: trimmed.description,
        provider: draft.provider,
        credential_id: credentialId,
        region: trimmed.region,
        /* `''` on either verb = the newest Ubuntu 24.04 LTS image at launch; the route validates a non-blank `ami-…`. */
        ami_id: trimmed.ami_id,
        /* `''` on either verb = resolve now (the route's contract), so a re-saved row re-resolves. The
           rest are the same VPC's, in order: a launch spreads across them all. */
        subnet_id: draft.subnet_ids[0] ?? '',
        extra_subnet_ids: draft.subnet_ids.slice(1),
        security_group_id: trimmed.security_group_id,
        root_volume_gb: volumeRaw === '' ? undefined : volume,
      }
      const saved = row ? await updateCloudConnection(row.id, body) : await createCloudConnection(body)
      await onSaved(saved)
      onClose()
    })
  }

  /* The row's credential may be one the options route no longer offers (two routes): still a label, so the
     select never renders blank over a saved id. */
  const providerType = spec?.credential_type ?? draft.provider
  const providerLabel = spec?.label ?? PROVIDER_LABELS[draft.provider] ?? draft.provider
  const credentialOptions = withCurrent(
    credentials.filter(c => credentialType(c) === providerType),
    row?.provider === draft.provider ? row.credential_id : '',
    id => ({ id, name: row?.credential_name || id, region: null, type: providerType, org_id: orgId ?? '' }),
  )
  const regionCatalogue = isAws
    ? regions.map(r => ({ id: r.code, name: r.name }))
    : cloudRegions.map(r => ({ id: r.value, name: r.label === r.value ? '' : r.label }))
  const regionOptions = withCurrent(regionCatalogue, draft.region, id => ({ id, name: '' }))
  /* "Add new…" files an AWS credential only (`AwsCredentialFields`); another cloud's is added under Credentials. */
  const offerNew = isAws && !foreignOrg
  const credentialHint = foreignOrg
    ? `Added by ${orgName || 'the organization'}’s admins under Credentials.`
    : !isAws && credentialOptions.length === 0 ? `Add a ${providerLabel} credential under Credentials.` : undefined
  const warning = isAws ? lookupError : regionLookupError || fieldLookupError
  const subnetOptions = draft.subnet_ids.reduceRight((items, current) => withCurrent(items, current, id => ({
    id, vpc_id: '', availability_zone: '', cidr_block: '', is_default: false, name: '',
  })), subnets ?? [])
  /* Auto resolves the region's default VPC: not offered once the lookup has ANSWERED without one. Unknown
     (not asked, in flight, refused) keeps it — the route resolves, or says why not. */
  const autoSubnet = subnets === null || subnets.some(s => s.is_default)
  const subnetVpc = subnetOptions.find(s => s.id === draft.subnet_ids[0])?.vpc_id ?? ''
  /* Same name twice in the list: the owner org tells them apart, else a short id. */
  const sameName = (c: CloudConnectionCredentialOption) =>
    credentialOptions.filter(other => other.name === c.name && other.id !== c.id)
  function credentialTell(c: CloudConnectionCredentialOption): string {
    const twins = sameName(c)
    if (twins.length === 0) return ''
    if (c.org_name && twins.every(other => other.org_name !== c.org_name)) return c.org_name
    return [c.org_name, c.id.slice(0, 8)].filter(Boolean).join(' · ')
  }
  /* The SAVED pin's age, as the Connections table states it, while the field still holds it (a typed id is a
     new pin, not that old). */
  const savedImage = row?.ami_id && draft.ami_id.trim() === row.ami_id ? connectionImage(row) : null
  const pinHint = savedImage && (
    <ConnectionImageText stale={savedImage.stale} testId="cloud-connection-pin-age">
      {`${savedImage.pinAge[0].toUpperCase()}${savedImage.pinAge.slice(1)}${savedImage.stale ? ' — re-pin due' : ''}.`}
    </ConnectionImageText>
  )
  /* AWS refused the PICKED credential — on the save (the field's error) or on the lookup (its warning). */
  const credentialRefused = credentialMode === 'pick' && draft.credential_id !== ''
    && (errors.credential_id !== undefined || (lookupError !== '' && credentialRefusal(lookupError) !== null))
  const lookupRefusal = plainRefusal(warning)
  const saveRefusal = errors.credential_id ? plainRefusal(errors.credential_id) : null
  const pickedCredentialName = credentialOptions.find(c => c.id === draft.credential_id)?.name ?? ''
  const groupOptions = withCurrent(securityGroups, draft.security_group_id, id => ({
    id, name: '', vpc_id: '', description: '',
  }))

  return (
    <FormDialog
      title={row ? `Edit ${row.name}` : foreignOrg && orgName ? `Add connection for ${orgName}` : 'Add connection'}
      /* Both rows are filed while the credential form is open: the button says so. */
      submitLabel={credentialMode === 'pick' ? (row ? 'Save' : 'Create') : `Save credential & ${row ? 'connection' : 'create'}`}
      busy={busy}
      error={formError || optionsError}
      onSubmit={() => void submit()}
      onClose={onClose}
      testId="cloud-connection-dialog"
      // Holds a typed access key and secret wherever it opens: a stray click must not drop them.
      modal
    >
      {/* THE CLOUD FIRST: it decides the credentials, regions and network below. */}
      <FormControl>
        <FormLabel id="cloud-connection-provider-label">Provider</FormLabel>
        <RadioGroup
          row
          aria-labelledby="cloud-connection-provider-label"
          value={draft.provider}
          onChange={e => chooseProvider(e.target.value as CloudProvider)}
          sx={{ alignItems: 'center' }}
        >
          {(providers ?? []).map(p => (
            <FormControlLabel key={p.key} value={p.key} control={<Radio size="small" />} label={p.label} disabled={busy} />
          ))}
        </RadioGroup>
      </FormControl>

      <FormTextField label="Name" error={errors.name} required id="cloud-connection-name" value={draft.name}
        onChange={v => set('name', v)} />

      {credentialMode !== 'pick' ? (
        <AwsCredentialFields
          key={credentialMode}
          ref={credentialForm}
          editId={credentialMode === 'edit' ? draft.credential_id : undefined}
          onPickExisting={pickExisting}
          onRegionChange={region => { if (!regionChosen.current) chooseRegion(region || DEFAULT_REGION) }}
          onLeave={onClose}
          disabled={busy}
        />
      ) : (
        <FormField
          label="Credential"
          error={errors.credential_id && plainRefusal(errors.credential_id).sentence}
          required
          hint={credentialHint}
        >
          <TextField
            id="cloud-connection-credential"
            select
            value={draft.credential_id}
            onChange={e => chooseCredential(e.target.value)}
            sx={fieldTapTargetSx}
            slotProps={{ select: { displayEmpty: true } }}
          >
            {credentialOptions.map(c => (
              <MenuItem key={c.id} value={c.id}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, minWidth: 0 }}>
                  {credentialMark(credentialType(c), providerLabel)}
                  <Box component="span" sx={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.name}</Box>
                  {credentialTell(c) && (
                    <Box component="span" sx={{ color: 'text.secondary', fontSize: '0.75rem' }}>{credentialTell(c)}</Box>
                  )}
                </Box>
              </MenuItem>
            ))}
            {/* Another org's credential is that org's admins' to add (`routes/credentials/` files a row
                in the caller's own org only), so the operator ordering for a tenant picks from its roster. */}
            {offerNew && <MenuItem value={NEW_CREDENTIAL} divider={false} sx={{ fontWeight: 600 }}>Add new…</MenuItem>}
          </TextField>
        </FormField>
      )}
      {/* ONE MESSAGE PER CAUSE: a save the credential was refused on is the field's error, and the lookup's
          warning (the same refusal, as a rule) yields to it. */}
      {warning && !errors.credential_id && (
        <Banner severity="warning" testId="cloud-connection-lookup-warning">
          {`${lookupRefusal.sentence} Auto still works.`}
          {lookupRefusal.code && lookupRefusal.aws && <Box><AwsCode code={lookupRefusal.code} aws={lookupRefusal.aws} /></Box>}
        </Banner>
      )}
      {/* THE WAY OUT OF A REFUSED CREDENTIAL, beside the refusal: its keys open in place of the select. A
          credential filed here moments ago is named once, so it is fixed or deleted rather than forgotten. */}
      {credentialRefused && (
        <Typography variant="body2" data-testid="cloud-connection-credential-fix">
          {saveRefusal?.code && saveRefusal.aws && <><AwsCode code={saveRefusal.code} aws={saveRefusal.aws} />{' · '}</>}
          {filedInline === draft.credential_id && (
            <>
              {`“${pickedCredentialName}” was saved to Credentials: `}
              <Link component="button" type="button" onClick={editCredential} disabled={busy}>edit it</Link>
              {' or '}
              <Link component="button" type="button" onClick={() => void deleteFiledCredential()} disabled={busy}>delete it</Link>.
            </>
          )}
          {filedInline !== draft.credential_id && (
            <Link component="button" type="button" onClick={editCredential}>Edit credential</Link>
          )}
        </Typography>
      )}

      {/* Required, so never folded: a refusal on it shows where it was made. */}
      <FormField label="Region" error={errors.region} required>
        <TextField
          id="cloud-connection-region"
          select
          value={draft.region}
          onChange={e => { regionChosen.current = true; chooseRegion(e.target.value) }}
          sx={fieldTapTargetSx}
        >
          {regionOptions.map(r => (
            <MenuItem key={r.id} value={r.id}>
              <Box component="span" sx={ID_SX}>{r.id}</Box>
              {r.name && <Box component="span" sx={{ ml: 1, color: 'text.secondary' }}>{r.name}</Box>}
            </MenuItem>
          ))}
        </TextField>
      </FormField>

      {/* THE FIRST VIEW IS WHAT A WORKING CONNECTION NEEDS; every optional field is folded here, never
          removed. An invalid field inside opens it (`ui/AdvancedSection`). */}
      <AdvancedSection testId="cloud-connection-advanced">
        {isAws ? <>
        <FormField label="Subnet" error={errors.subnet_ids} hint="Pick one per zone to spread machines.">
          <TextField
            id="cloud-connection-subnet"
            select
            value={draft.subnet_ids}
            // Auto (`''`) clears the list; any other click toggles one id, in pick order.
            onChange={e => {
              const next = e.target.value as unknown as string[]
              set('subnet_ids', next.includes('') ? [] : next)
            }}
            sx={fieldTapTargetSx}
            // An empty list (Auto) still reads, so the label floats over it rather than sitting on it.
            slotProps={{
              select: {
                multiple: true,
                displayEmpty: true,
                renderValue: value => {
                  const ids = value as string[]
                  if (ids.length > 0) return <Box component="span" sx={ID_SX}>{ids.join(', ')}</Box>
                  return autoSubnet ? 'Auto — default VPC' : `Pick a subnet — no default VPC in ${draft.region}`
                },
              },
              inputLabel: { shrink: true },
            }}
          >
            {autoSubnet && <MenuItem value="">Auto — default VPC</MenuItem>}
            {subnetOptions.map(s => (
              <MenuItem
                key={s.id}
                value={s.id}
                disabled={subnetVpc !== '' && s.vpc_id !== '' && s.vpc_id !== subnetVpc}
                sx={ID_SX}
              >
                {s.availability_zone ? subnetLabel(s) : s.id}
              </MenuItem>
            ))}
          </TextField>
        </FormField>

        <FormField label="Security group" error={errors.security_group_id}>
          <TextField
            id="cloud-connection-security-group"
            select
            value={draft.security_group_id}
            onChange={e => set('security_group_id', e.target.value)}
            sx={fieldTapTargetSx}
            slotProps={{ select: { displayEmpty: true }, inputLabel: { shrink: true } }}
          >
            <MenuItem value="">Auto — jaah-node</MenuItem>
            {groupOptions.map(g => (
              <MenuItem key={g.id} value={g.id} sx={ID_SX}>{g.name ? `${g.id} · ${g.name}` : g.id}</MenuItem>
            ))}
          </TextField>
        </FormField>
        </> : genericFields.map(f => (
          <NetworkField key={f.name} field={f} value={draft.network[f.name]} options={fieldOptions[f.name] ?? []}
            onChange={value => setNetwork(f.name, value)} />
        ))}

        <FormTextField
          label="Root volume, GB"
          error={errors.root_volume_gb}
          id="cloud-connection-volume"
          value={draft.root_volume_gb}
          // `numeric` raises a digit keypad; not `type="number"` (spinner nudges).
          slotProps={{ htmlInput: { inputMode: 'numeric' } }}
          onChange={v => set('root_volume_gb', v)}
          sx={ID_SX}
        />

        <FormTextField label="Description" error={errors.description} id="cloud-connection-description"
          value={draft.description} onChange={v => set('description', v)} />

        {/* Last: a pin is the exception (contract decision 2 — the launch resolves the image itself). */}
        <FormTextField
          label={isAws ? 'Image (AMI id)' : 'Image'}
          error={errors.ami_id}
          hint={pinHint || undefined}
          id="cloud-connection-ami"
          value={draft.ami_id}
          // Blank is a choice, not a gap: say what it launches.
          placeholder={`Newest ${autoImage || 'Ubuntu LTS'} at each launch`}
          onChange={v => set('ami_id', v)}
          sx={ID_SX}
        />

        {/* NodeLink: linked, not set (it lives in Networks > NodeLink). */}
        {showNodeLink && (
          <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="cloud-connection-nodelink">
            Shared storage:{' '}
            <Link component={RouterLink} to={NODELINK_STORE_URL} onClick={onClose}>NodeLinks</Link>.
          </Typography>
        )}
      </AdvancedSection>
      {/* Its own dialog, portalled; `FormDialog` stops its submit from reaching this form. */}
      {confirmDialog}
    </FormDialog>
  )
}

/** One field of another cloud's network, drawn from its `ConnectionField` alone: a typed value, a select, or a
 *  multi-select; blank = `auto_label`, resolved on save. A saved value the lookup did not list still reads. */
function NetworkField({ field, value, options, onChange }: {
  field: ConnectionField
  value: string | string[] | undefined
  options: LookupOption[]
  onChange: (value: string | string[]) => void
}): JSX.Element {
  const id = `cloud-connection-field-${field.name}`
  const hint = field.hint || undefined
  if (field.kind === 'text' || field.kind === 'int') {
    return (
      <FormTextField label={field.label} hint={hint} required={field.required} id={id}
        value={typeof value === 'string' ? value : ''} placeholder={field.auto_label ?? undefined} onChange={onChange}
        slotProps={field.kind === 'int' ? { htmlInput: { inputMode: 'numeric' } } : undefined} sx={ID_SX} />
    )
  }
  const auto = field.auto_label ?? ''
  const listed = options.map(o => ({ id: o.value, name: o.label }))
  if (field.kind === 'multiselect') {
    const picked = Array.isArray(value) ? value : []
    const items = picked.reduceRight((list, current) => withCurrent(list, current, v => ({ id: v, name: v })), listed)
    return (
      <FormField label={field.label} hint={hint} required={field.required}>
        <TextField
          id={id}
          select
          value={picked}
          // Auto (`''`) clears the list; any other click toggles one value, in pick order.
          onChange={e => {
            const next = e.target.value as unknown as string[]
            onChange(next.includes('') ? [] : next)
          }}
          sx={fieldTapTargetSx}
          slotProps={{
            select: {
              multiple: true,
              displayEmpty: true,
              renderValue: v => ((v as string[]).length > 0 ? (v as string[]).join(', ') : auto),
            },
            inputLabel: { shrink: true },
          }}
        >
          {auto && <MenuItem value="">{auto}</MenuItem>}
          {items.map(o => <MenuItem key={o.id} value={o.id}>{o.name}</MenuItem>)}
        </TextField>
      </FormField>
    )
  }
  const current = typeof value === 'string' ? value : ''
  return (
    <FormField label={field.label} hint={hint} required={field.required}>
      <TextField
        id={id}
        select
        value={current}
        onChange={e => onChange(e.target.value)}
        sx={fieldTapTargetSx}
        slotProps={{ select: { displayEmpty: true }, inputLabel: { shrink: true } }}
      >
        {auto && <MenuItem value="">{auto}</MenuItem>}
        {withCurrent(listed, current, v => ({ id: v, name: v })).map(o => <MenuItem key={o.id} value={o.id}>{o.name}</MenuItem>)}
      </TextField>
    </FormField>
  )
}
