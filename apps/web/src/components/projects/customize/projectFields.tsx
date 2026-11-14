import type { JSX } from 'react'
import type { ProjectFormState } from '../projectForm'
import { DetailCell } from '../../../ui/detail/DetailPage'
import FormTextField, { type FormTextFieldProps } from '../../../ui/FormTextField'

/* The project edit page's field plumbing shared by its tabs and the Customize sections (`pages/ProjectEditPage.tsx`,
 * `customize/BranchesSection.tsx`, `customize/TestingSection.tsx`): the reader-docs pages the field explanations
 * link, the draft's string keys, and the one text cell bound to the draft. */

/* The reader-docs pages the project fields' explanations link: the field table, and the concepts behind it. */
export const PROJECT_FIELDS_DOCS = 'console-reference/directory/projects'
export const HOW_PROJECTS_WORK_DOCS = 'using-jaah/projects/how-projects-work'

/* Only string-valued keys may be listed, type-enforced: `.trim()` on `String(null)`/`String(false)` is
 * truthy, so a non-string entry would never fire. */
export type StringFieldKey = {
  [K in keyof ProjectFormState]-?: ProjectFormState[K] extends string ? K : never
}[keyof ProjectFormState]

/* ONE text cell of the page's panels: a `DetailCell` around a `FormTextField` bound to one string key of the draft
 * (value, patch, error). Bound to the draft once per render (`projectTextCell`) so each site says only what differs;
 * a plain function, not a component, so the inputs keep their identity across renders. The `id` is
 * `project-<name, - for _>` unless the site spells it. */
export type TextCellProps = {
  name: StringFieldKey
  id?: string
  basis: string
  max: string
} & Pick<FormTextFieldProps, 'label' | 'help' | 'placeholder' | 'type' | 'readOnly' | 'warning'>

export type TextCell = (props: TextCellProps) => JSX.Element

export function projectTextCell(
  form: ProjectFormState,
  patch: (next: Partial<ProjectFormState>) => void,
  errorFor: (key: StringFieldKey) => string | undefined,
): TextCell {
  return ({ name, id, basis, max, ...rest }: TextCellProps) => (
    <DetailCell basis={basis} max={max}>
      <FormTextField
        id={id ?? `project-${name.replace(/_/g, '-')}`}
        {...rest}
        value={form[name]}
        onChange={v => patch({ [name]: v })}
        error={errorFor(name)}
      />
    </DetailCell>
  )
}
