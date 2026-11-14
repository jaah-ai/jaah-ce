import type { JSX } from 'react'
import { FormTextField } from '../../ui/FormTextField'
import type { TemplateInput } from './api'

/* A TEMPLATE'S INPUTS — one field per `{{input: Label}}` placeholder in its body (`_card.inputs`), answered on Start
 * (`StartTemplateDialog`) or stored on an automation's Task action (`taskAction.tsx`). The answers are keyed by label;
 * the server fills the body with them (`app/task_templates.py::fill_inputs`). */

/** The body's input marks: the server's `INPUT_TOKEN` (`app/task_templates.py`), spelled again for the preview. */
const INPUT_MARK = /\{\{\s*input(\?)?\s*:\s*([^{}\n]{1,80}?)\s*\}\}/g
const squash = (label: string) => label.split(/\s+/).filter(Boolean).join(' ')

/** `body` as the task will read it: each mark filled with its answer, as `fill_inputs` does; a blank optional one
 *  reads as the server writes it (`(not given)`), a blank required one names its input (`‹Label›`) — never the
 *  raw mark. */
export function previewBody(body: string, values: Record<string, string>): string {
  const given = new Map(Object.entries(values).map(([label, value]) => [squash(label), value.trim()]))
  // A label required by any of its marks is required everywhere (`inputs_of`).
  const required = new Set([...body.matchAll(INPUT_MARK)].filter(m => !m[1]).map(m => squash(m[2])))
  return body.replace(INPUT_MARK, (_mark, _optional: string | undefined, label: string) =>
    given.get(squash(label)) || (required.has(squash(label)) ? `‹${squash(label)}›` : '(not given)'))
}

export const missingInputs = (inputs: readonly TemplateInput[], values: Record<string, string>): boolean =>
  inputs.some(i => i.required && !(values[i.label] ?? '').trim())

export default function TemplateInputFields({ inputs, values, onChange, disabled, error }: {
  inputs: readonly TemplateInput[]
  values: Record<string, string>
  onChange: (values: Record<string, string>) => void
  disabled?: boolean
  error?: string
}): JSX.Element {
  return (
    <>
      {inputs.map((input, index) => (
        <FormTextField
          key={input.label}
          /* An optional input says so under the box: a required one carries the asterisk, and nothing else tells them
             apart. Not in the label, which may already end in its own brackets. */
          label={input.label}
          hint={input.required ? undefined : 'Optional'}
          required={input.required}
          help="Fills this input's mark in the template's brief."
          value={values[input.label] ?? ''}
          onChange={v => onChange({ ...values, [input.label]: v })}
          disabled={disabled}
          error={index === 0 ? error : undefined}
          multiline
          maxRows={6}
          fullWidth
          testId={`template-input-${index}`}
          // Always above the box: a long label sitting inside an empty field reads as a placeholder, not a label.
          slotProps={{ inputLabel: { shrink: true } }}
        />
      ))}
    </>
  )
}
