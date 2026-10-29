import { forwardRef, useEffect, useImperativeHandle, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { apiDetail, ApiError } from '../../../api'
import { useAuth } from '../../../auth'
import { sealingDestination } from '../../../lib/credentialSeal'
import FormTextField from '../../../ui/FormTextField'
import {
  createCredential,
  getCredential,
  updateCredentialKeys,
  type Credential,
  type CredentialOwnership,
  type FieldSpec,
} from '../../credentials/api'
import { buildSealedConfig, inlineFieldError, RegistryFieldInput, useSealKey, withoutKey } from '../../credentials/credentialForm'
import { createsOrgRows, mintCredentialId, SEAL_UNAVAILABLE } from '../../credentials/ownership'
import OwnershipRadios from '../../credentials/OwnershipRadios'
import { useCredentialTypes } from '../../credentials/useCredentialTypes'

/* The INLINE AWS credential form the connection dialog swaps in for its Credential select — a NEW one
 * ("Add new…"), or the picked one's keys to fix after AWS refused them ("Edit credential", `editId`). The
 * registry's fields for type `aws` (`useCredentialTypes` — never a TS copy of the server registry), secrets
 * sealed exactly as `pages/credentials/CredentialEditPage.tsx` seals them (the row's id in the
 * AAD, one envelope per secret, plaintext never on the wire). New: the owner choice when the caller may
 * create org rows. Edit: a blank secret keeps the stored one (the PATCH merges per key), and a NodeVault row
 * — sealed under a key only the org's nodes hold — is sent to its own page. The dialog's Create/Save calls
 * `save()` FIRST and files the connection under the returned id. */

/** The registry's AWS type slug (`apps/api/app/credentials/registry.py`). */
const AWS_TYPE = 'aws'

/** What the dialog drives: `save()` resolves the created or updated row, or `null` when a refusal was shown
 *  here (nothing to add to the dialog's own banner). Any other rejection propagates whole. */
export type AwsCredentialHandle = {
  save: () => Promise<Credential | null>
}

export type AwsCredentialFieldsProps = {
  /** The credential to edit; absent = a new one. */
  editId?: string
  /** "Pick existing" / "Cancel": back to the select. */
  onPickExisting: () => void
  /** The AWS `region` field as typed, so the dialog's own Region can follow it. */
  onRegionChange?: (region: string) => void
  /** A link that leaves the dialog (to the credential's own page) closes it first. */
  onLeave?: () => void
  disabled?: boolean
}

const AwsCredentialFields = forwardRef<AwsCredentialHandle, AwsCredentialFieldsProps>(
  function AwsCredentialFields({ editId, onPickExisting, onRegionChange, onLeave, disabled = false }, ref) {
    const { admin } = useAuth()
    const { types, loading: typesLoading, error: typesError } = useCredentialTypes()
    const spec = types.find(t => t.slug === AWS_TYPE)
    const fields = (spec?.fields ?? []) as FieldSpec[]
    const editing = editId !== undefined

    const choosesOwner = !editing && createsOrgRows(admin)
    /* ORG BY DEFAULT where the caller may choose: a connection is an org asset, and a Personal credential
       would hide the connection from the org's other admins. */
    const [ownership, setOwnership] = useState<CredentialOwnership>(choosesOwner ? 'org' : 'personal')
    const [name, setName] = useState('')
    /* Type-specific values by field key; a field's `default` applies until the operator types over it
       (`values` holds only what was typed, so the registry stays the one home of the default). */
    const [values, setValues] = useState<Record<string, string>>({})
    const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})

    /* Edit: the stored row (non-secret values, which secrets are set). `null` until it answers. */
    const [record, setRecord] = useState<Credential | null>(null)
    const [loadError, setLoadError] = useState('')
    useEffect(() => {
      if (editId === undefined) return
      let live = true
      void (async () => {
        try {
          const row = await getCredential(editId)
          if (!live) return
          setRecord(row)
          setName(row.name)
          setValues({ ...row.config })
        } catch (err) {
          if (live) setLoadError(apiDetail(err, 'Failed to load the credential.'))
        }
      })()
      return () => { live = false }
    }, [editId])
    const readOnly = record?.editable === false
    const sealedOnNodes = record?.nodevault === true

    /* New: minted ONCE per mount and sent with the POST (every envelope's AAD carries it); a 409 on it
       surfaces like any other refusal. `''` = no id could be minted, so nothing sealed here could be bound to
       a row: the form says so and `save()` files nothing. Edit: the row's own id. The seal key is fetched
       once and held for the form's life. */
    const [newId] = useState(mintCredentialId)
    const credentialId = editId ?? newId
    const sealUnavailable = credentialId === ''
    const sealKey = useSealKey()

    const valueOf = (field: FieldSpec) => values[field.key] ?? field.default ?? ''
    const storedSecret = (field: FieldSpec) => field.kind === 'secret' && (record?.secrets_set.includes(field.key) ?? false)

    const setValue = (key: string, value: string) => {
      setValues(current => ({ ...current, [key]: value }))
      setFieldErrors(current => withoutKey(current, key))
      if (key === 'region') onRegionChange?.(value)
    }

    function validate(): Record<string, string> {
      const errors: Record<string, string> = {}
      if (!name.trim()) errors.name = 'Name is required.'
      for (const field of fields) {
        if (field.required && !valueOf(field).trim() && !storedSecret(field)) errors[field.key] = `${field.label} is required.`
      }
      return errors
    }

    useImperativeHandle(ref, () => ({
      async save() {
        if (editing && record === null) return null
        /* Nothing here to change: the connection is filed on the credential as it stands. */
        if (record !== null && (readOnly || sealedOnNodes)) return record
        if (sealUnavailable) return null
        const errors = validate()
        setFieldErrors(errors)
        if (Object.keys(errors).length > 0 || spec === undefined) return null
        // The destination is computed from the same values the request carries.
        const sent: Record<string, string> = {}
        for (const field of fields) sent[field.key] = valueOf(field)
        const destination = sealingDestination(spec, sent)
        const config = await buildSealedConfig({ fields, values: sent, credentialId, destination, sealKey })
        try {
          if (record !== null) {
            return await updateCredentialKeys(record.id, { name: name.trim(), config })
          }
          return await createCredential({
            id: credentialId,
            name: name.trim(),
            type: AWS_TYPE,
            description: null,
            config,
            ...(choosesOwner ? { ownership } : {}),
          })
        } catch (err) {
          const inline = err instanceof ApiError ? inlineFieldError(fields, apiDetail(err, '')) : null
          if (inline === null) throw err
          setFieldErrors(inline)
          return null
        }
      },
    }))

    const inputId = (key: string) => `cloud-connection-credential-${key}`
    const pageLink = editId !== undefined && (
      <Link component={RouterLink} to={`/credentials/${editId}`} onClick={onLeave}>its page</Link>
    )
    const locked = disabled || readOnly || sealedOnNodes || (editing && record === null)

    return (
      <Box data-testid={editing ? 'edit-aws-credential' : 'new-aws-credential'} sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <Box sx={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 1 }}>
          <Typography variant="subtitle2">{editing ? 'Edit AWS credential' : 'New AWS credential'}</Typography>
          <Link component="button" type="button" variant="body2" onClick={onPickExisting} disabled={disabled}>
            {editing ? 'Cancel edit' : 'Pick existing'}
          </Link>
        </Box>

        {loadError && <Typography color="error" variant="body2" role="alert">{loadError}</Typography>}
        {readOnly && (
          <Typography variant="body2" role="alert">Only its owner or an admin can change this credential.</Typography>
        )}
        {sealedOnNodes && (
          <Typography variant="body2" role="alert">Sealed with NodeVault: change its keys on {pageLink}.</Typography>
        )}

        <FormTextField
          id={inputId('name')}
          label="Name"
          error={fieldErrors.name}
          required
          value={name}
          disabled={locked}
          autoComplete="off"
          onChange={value => {
            setName(value)
            setFieldErrors(current => withoutKey(current, 'name'))
          }}
          slotProps={{ formHelperText: fieldErrors.name ? { role: 'alert' } : undefined }}
        />

        {sealUnavailable && <Typography color="error" variant="body2" role="alert">{SEAL_UNAVAILABLE}</Typography>}
        {typesError && <Typography color="error" variant="body2" role="alert">{typesError}</Typography>}
        {!typesLoading && !typesError && spec === undefined && (
          <Typography color="error" variant="body2" role="alert">The AWS credential type is not available.</Typography>
        )}

        {fields.map(field => (
          <RegistryFieldInput
            key={field.key}
            field={field}
            id={inputId(field.key)}
            error={fieldErrors[field.key]}
            required={field.required && !storedSecret(field)}
            disabled={locked}
            value={valueOf(field)}
            hint={storedSecret(field) ? 'Leave blank to keep the stored value.' : undefined}
            onChange={value => setValue(field.key, value)}
          />
        ))}

        {choosesOwner && (
          <OwnershipRadios
            labelId="cloud-connection-credential-ownership-label"
            value={ownership}
            onChange={setOwnership}
            disabled={disabled}
            oneLine
          />
        )}
      </Box>
    )
  },
)

export default AwsCredentialFields
