import type { JSX } from 'react'
import FormTextField from '../../ui/FormTextField'

/** The project's Name: the one field the create wizard and the edit page draw alike. `help` replaces the
 *  default hover where the name does more (a Jaah-made project's repository is named after it). */
export function ProjectNameField({ value, onChange, error, help = 'Shown on tasks, boards and the project list.' }: {
  value: string
  onChange: (value: string) => void
  error?: string
  help?: string
}): JSX.Element {
  return <FormTextField id="project-name" label="Name" help={help} required requiredMark={false} value={value} onChange={onChange} error={error} />
}
