import Chip, { type ChipProps } from '@mui/material/Chip'
import type { JSX } from 'react'

/* A MARK drawn after an entity's name: a small outlined chip with its sentence as the title. One home for each
 * mark's word and sentence. */
function MarkChip({ label, title, testId, color }: {
  label: string
  title: string
  testId: string
  color?: ChipProps['color']
}): JSX.Element {
  return (
    <Chip size="small" variant="outlined" color={color} label={label} title={title} data-testid={testId}
      sx={{ ml: 0.5, verticalAlign: 'middle', flexShrink: 0 }} />
  )
}

/** The built-in project: the per-org project a `system_kind` sets (`types.ts::Project.system_kind`). */
export const SystemChip = (): JSX.Element =>
  <MarkChip label="System" title="Built-in: cannot be deleted or given a repository" testId="system-chip" />

/** A credential's home project (`CredentialAccess.projects[].home`): moved from the Details tab's Home, never detached. */
export const HomeChip = (): JSX.Element =>
  <MarkChip label="Home" title="Change under Details › Home" testId="home-chip" />

/** A project's primary repository (`types.ts::ProjectRepo.is_primary`). */
export const PrimaryChip = (): JSX.Element =>
  <MarkChip label="Primary" color="primary" title="Gate, stack and ports run here" testId="primary-chip" />

/** A repository row that Create or Save makes (`projectForm.ts::RepoDraft.create`). */
export const NewRepoChip = (): JSX.Element =>
  <MarkChip label="New" title="Created private, with main and develop" testId="new-repo-chip" />

/** An org in HIPAA mode (`Org.hipaa.enabled`): the org header and the operator's roster. */
export const HipaaChip = ({ baaDate }: { baaDate?: string | null }): JSX.Element => (
  <MarkChip label="HIPAA" color="info" testId="hipaa-chip"
    title={baaDate ? `HIPAA mode is on. BAA effective ${baaDate}.` : 'HIPAA mode is on.'} />
)

/** A member or group the org's identity provider manages over SCIM (`ManagedUser.scim_managed`,
 *  `GroupSummary.scim_managed`): the People and Groups rosters and the group's header. */
export const ScimChip = (): JSX.Element =>
  <MarkChip label="SCIM" testId="scim-mark" title="Managed by your identity provider. Edits here are overwritten at the next sync." />

/** A disabled account (`ManagedUser.disabled_at`): the People roster. */
export const DisabledChip = (): JSX.Element =>
  <MarkChip label="Disabled" color="error" testId="user-disabled" title="Cannot sign in" />

/** A person who turned the org's invitation down (`ManagedUser.invitation_status`): the People roster. */
export const DeclinedChip = (): JSX.Element =>
  <MarkChip label="Declined" testId="user-declined" title="Invitation declined" />

/** A MicroVM node (`Node.microvm`): a project's Slots › Nodes rows. */
export const MicrovmChip = (): JSX.Element =>
  <MarkChip label="MicroVM" color="info" testId="microvm-chip" title="Runs each task in its own MicroVM" />
