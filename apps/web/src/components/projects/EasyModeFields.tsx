import type { JSX, ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { apiDetail, get } from '../../api'
import { useSettled } from '../../hooks/useSettled'
import ChoiceCard, { CHOICE_GRID_SX } from '../addNode/ChoiceCard'
import FormTextField from '../../ui/FormTextField'
import { AppWindowIcon, GlobeIcon, MegaphoneIcon, RepoBranchIcon, ShapesIcon, SparkleIcon } from '../../ui/icons'
import { FONT_WEIGHT } from '../../theme'

/* The New project page's questions and its description fields (`pages/ProjectCreateWizard.tsx`), for every org:
 * "What are you making?" → "Is it new or existing?" (an app only: a business initiative is always new) → "What kind
 * of app?" (a new app, on its details step). Each is a group of large pickable cards (`QuestionCards`): an icon, the
 * answer, and one line on when it fits best. The answers become the create's `intake` (`intakeKind`,
 * `apps/api/app/project_intake.py`): Jaah makes the repository for a new app or a business initiative; an existing
 * app connects the person's own. A new web app then asks Name, shows the address it will get
 * (`GET /api/hosted-apps/slug`, `EasyModeFields`), and asks the person to describe the app in their own words: no
 * technology choice, since Jaah builds it from one starter. A business initiative is described the same way
 * (`DescriptionField`). The description seeds the App or Business Wizard (`apps/api/app/project_onboarding.py`). */

export type ProjectWhat = 'app' | 'business'
export type AppAge = 'new' | 'existing'
export type AppKind = 'web' | 'other'
/** The `ProjectIntake.kind` values this page sends (`apps/api/app/project_intake.py`). */
export type IntakeKind = 'web_app' | 'other_app' | 'business' | 'existing_app'

export type QuestionChoice<V extends string> = { value: V; label: string; hint: string; icon: ReactNode; testId: string }
export type Question<V extends string> = { title: string; choices: readonly QuestionChoice<V>[] }

export const WHAT_QUESTION: Question<ProjectWhat> = {
  title: 'What are you making?',
  choices: [
    {
      value: 'app', label: 'App / Website', icon: <AppWindowIcon />, testId: 'project-what-app',
      hint: "Best when you're building software: a product, a site or an internal tool that needs code and gets deployed.",
    },
    {
      value: 'business', label: 'Business / Sales / Marketing Initiative', icon: <MegaphoneIcon />, testId: 'project-what-business',
      hint: "Best for campaigns, outreach, research and planning. No code involved, so there's nothing technical to set up.",
    },
  ],
}

export const AGE_QUESTION: Question<AppAge> = {
  title: 'Is it new or existing?',
  choices: [
    {
      value: 'new', label: 'New', icon: <SparkleIcon />, testId: 'project-age-new',
      hint: "Best when you're starting from an idea. Jaah creates the repository and builds the first version for you.",
    },
    {
      value: 'existing', label: 'Existing', icon: <RepoBranchIcon />, testId: 'project-age-existing',
      hint: 'Best when the code already lives in a repository. Connect it and Jaah learns the project and carries on from there.',
    },
  ],
}

export const KIND_QUESTION: Question<AppKind> = {
  title: 'What kind of app?',
  choices: [
    { value: 'web', label: 'Web app', icon: <GlobeIcon />, hint: 'Runs in a browser, hosted by Jaah', testId: 'project-kind-web' },
    { value: 'other', label: 'Something else', icon: <ShapesIcon />, hint: 'Mobile, desktop, scripts, anything else', testId: 'project-kind-other' },
  ],
}

/** The answers' `intake.kind`, or null while one the path needs is unanswered. */
export function intakeKind(what: ProjectWhat | null, age: AppAge | null, kind: AppKind | null): IntakeKind | null {
  if (what === 'business') return 'business'
  if (what === null || age === null) return null
  if (age === 'existing') return 'existing_app'
  return kind === 'web' ? 'web_app' : kind === 'other' ? 'other_app' : null
}

/** One question as a radio group of `ChoiceCard`s, labelled by `labelledBy` (the step's heading or the field's
 *  label). Native radios: one tab stop, arrows move the pick. A choice named in `off` is disabled, its reason under
 *  its hint. `compact` lays the cards out two to a row (a short answer beside its neighbour); otherwise one per row.
 *  `groupProps` and `onPicked` let the page move on after a pointer pick (`hooks/usePickAdvance.ts`). */
export function QuestionCards<V extends string>({
  question, value, onPick, off, name, labelledBy, compact = false, groupProps,
}: {
  question: Question<V>
  value: V | null
  onPick: (v: V) => void
  off?: Partial<Record<V, string>>
  /** The radio group's name and test id, one per question. */
  name: string
  labelledBy: string
  compact?: boolean
  groupProps?: { onPointerDown: () => void; onKeyDown: () => void }
}): JSX.Element {
  return (
    <Box
      role="radiogroup" aria-labelledby={labelledBy} data-testid={name} {...groupProps}
      sx={compact ? CHOICE_GRID_SX : { display: 'flex', flexDirection: 'column', gap: 1.5 }}
    >
      {question.choices.map(c => (
        <ChoiceCard
          key={c.value} name={name} value={c.value} checked={value === c.value} disabled={off?.[c.value] !== undefined}
          onSelect={() => onPick(c.value)} title={c.label} icon={c.icon} testId={c.testId}
        >
          <Box component="span" sx={{ display: 'block' }}>{c.hint}</Box>
          {off?.[c.value] && (
            <Box component="span" sx={{ display: 'block', color: 'text.primary' }} data-testid={`${c.testId}-reason`}>{off[c.value]}</Box>
          )}
        </ChoiceCard>
      ))}
    </Box>
  )
}

/** `project_intake.MAX_DESCRIPTION_CHARS`. */
export const DESCRIPTION_MAX = 4000

/** `project_intake.DESCRIBE_YOUR_APP`: a new web app's refusal while its description is blank. */
export const DESCRIBE_YOUR_APP = 'Describe your app.'

/** `project_intake.DESCRIBE_YOUR_INITIATIVE`: a business initiative's refusal while its description is blank. */
export const DESCRIBE_YOUR_INITIATIVE = 'Describe the initiative.'

/** The slug route's cap on a name (`routes/hosted_apps.py::preview_slug`). */
const NAME_MAX = 200

/** How long a typed name settles before its address is asked for. */
const SLUG_SETTLE_MS = 400

/** The address an app named `name` gets; null while the name is blank. */
export function useEasyAddress(name: string) {
  const settled = useSettled(name.trim(), SLUG_SETTLE_MS)
  return useQuery({
    queryKey: ['hosted-app-slug', settled],
    queryFn: () => get<{ slug: string; address: string }>(`/api/hosted-apps/slug?name=${encodeURIComponent(settled)}`),
    enabled: settled !== '',
    retry: false,
    staleTime: 30_000,
  })
}

/** A new app's details: Name, then (a web app) the address it will get, then `kind` (the "What kind of app?"
 *  cards), then (a web app) its description. Another kind of app gets no address and sends no description. */
export function EasyModeFields({
  name, onName, nameError, kind, web, description, onDescription, descriptionError,
}: {
  name: string
  onName: (v: string) => void
  /** The server's refusal of the name, under the field. */
  nameError?: string
  /** The "What kind of app?" field, drawn between the name and the description. */
  kind: ReactNode
  /** A web app: shows the address and asks for the description. */
  web: boolean
  description: string
  onDescription: (v: string) => void
  /** Why the description cannot go yet, under the field. */
  descriptionError?: string
}): JSX.Element {
  const address = useEasyAddress(web ? name : '')
  const typed = name.trim() !== ''
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2.5 }}>
      <FormTextField
        id="easy-app-name" label="Name" required requiredMark={false}
        help={web ? 'Names the project, its app and its private repository.' : 'Names the project and its private repository (main and develop).'}
        error={nameError} value={name} onChange={onName} slotProps={{ htmlInput: { maxLength: NAME_MAX } }}
      />
      {/* The address follows the name: nothing to show before one is typed. */}
      {web && typed && (
        <Box data-testid="easy-address" aria-live="polite">
          <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.medium }}>Address</Typography>
          <Typography variant="body2" sx={{ color: address.isError ? 'error.main' : 'text.secondary', overflowWrap: 'anywhere' }}>
            {address.isError ? apiDetail(address.error, 'The address could not be read.')
              : address.data ? `https://${address.data.address}`
                : 'Finding a free address…'}
          </Typography>
        </Box>
      )}
      {kind}
      {web && (
        <DescriptionField
          id="easy-app-description" label="Describe your app"
          help="What it does and who it's for, in your own words. Jaah asks about anything missing, then builds it."
          placeholder="A booking site for my dog-walking business: owners pick a time slot, pay, and get a reminder."
          value={description} onChange={onDescription} error={descriptionError}
        />
      )}
    </Box>
  )
}

/** The required, multiline "describe it in your own words" field (capped at `DESCRIPTION_MAX`); `id` is also its
 *  test id. */
export function DescriptionField({
  id, label, help, placeholder, value, onChange, error,
}: {
  id: string
  label: string
  help: string
  placeholder: string
  value: string
  onChange: (v: string) => void
  /** Why the description cannot go yet, under the field. */
  error?: string
}): JSX.Element {
  return (
    <FormTextField
      id={id} testId={id} label={label} help={help} placeholder={placeholder}
      required requiredMark={false} multiline minRows={4} error={error}
      value={value} onChange={onChange} slotProps={{ htmlInput: { maxLength: DESCRIPTION_MAX } }}
    />
  )
}
