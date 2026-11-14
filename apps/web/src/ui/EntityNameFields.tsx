import type { JSX } from 'react'
import FormTextField, { type FormTextFieldProps } from './FormTextField'
import { TEXT_INPUT } from './formDensity'

/* The ONE home of a record's "Name" + "Description" pair (the entity shape's two human fields,
 * the coding standards), so an edit page or dialog never re-spells their label, cap or shape.
 *
 * - `EntityNameField`: `Name`, required, one line, no spell-check or autofill (`TEXT_INPUT`).
 * - `EntityDescriptionField`: `Description`, a textarea of at least two rows.
 * - `EntityNameFields`: both, adjacent; `idPrefix` gives them `<prefix>-name`/`<prefix>-description`, and
 *   `disabled` greys both. A form whose Description is not the plain textarea (another field between them, a
 *   fixed `rows`, a single line, a rendered preview) uses the Name half alone.
 *
 * Every other `FormTextField` prop passes through and wins over these defaults; `slotProps.htmlInput` merges
 * over the defaults rather than replacing them. `maxLength` is the column's cap as the server enforces it
 * (the entity shape's columns are plain text, so there is no default: a form states only a cap the server has). */

type CapProps = Omit<FormTextFieldProps, 'label'> & {
  /** The input's `maxLength`, when the server enforces one. */
  maxLength?: number
}

/** The fallback explanations; a form with something more specific to say passes its own `help`. */
const NAME_HELP = 'Identifies this record in lists and pickers.'
const DESCRIPTION_HELP = 'A note shown with this record; changes no behaviour.'

export type EntityNameFieldProps = CapProps
export type EntityDescriptionFieldProps = CapProps

function capped(slotProps: FormTextFieldProps['slotProps'], base: Record<string, unknown>, maxLength: number | undefined) {
  const slots = slotProps as Record<string, Record<string, unknown> | undefined> | undefined
  return {
    ...slots,
    htmlInput: { ...base, ...(maxLength === undefined ? {} : { maxLength }), ...slots?.htmlInput },
  }
}

export function EntityNameField({ maxLength, required = true, slotProps, ...rest }: EntityNameFieldProps): JSX.Element {
  return (
    <FormTextField
      label="Name"
      help={NAME_HELP}
      required={required}
      {...rest}
      slotProps={capped(slotProps, TEXT_INPUT.htmlInput, maxLength)}
    />
  )
}

export function EntityDescriptionField({ maxLength, slotProps, ...rest }: EntityDescriptionFieldProps): JSX.Element {
  return (
    <FormTextField
      label="Description"
      help={DESCRIPTION_HELP}
      multiline
      minRows={2}
      {...rest}
      slotProps={capped(slotProps, {}, maxLength)}
    />
  )
}

export function EntityNameFields({ idPrefix, disabled, name, description }: {
  /** Ids `<idPrefix>-name` and `<idPrefix>-description`; none without it. */
  idPrefix?: string
  disabled?: boolean
  name: EntityNameFieldProps
  description: EntityDescriptionFieldProps
}): JSX.Element {
  return (
    <>
      <EntityNameField id={idPrefix && `${idPrefix}-name`} disabled={disabled} {...name} />
      <EntityDescriptionField id={idPrefix && `${idPrefix}-description`} disabled={disabled} {...description} />
    </>
  )
}

export default EntityNameFields
