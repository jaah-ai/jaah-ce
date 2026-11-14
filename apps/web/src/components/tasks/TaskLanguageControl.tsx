import { put } from '../../api'
import { useSubmit } from '../../hooks/useSubmit'
import { DEFAULT_LANGUAGE, languageName, languageOptions } from '../../lib/languages'
import type { PipelineTask } from '../../types'
import InlineSelect from '../../ui/InlineSelect'
import { grid } from '../../theme'

/** The task's language in its Details field: the name, with "Change…" where `editable` (a top-level task the
 *  caller may edit — children carry their epic's). A pick covers the whole subtree and is never re-detected over. */
export default function TaskLanguageControl({
  task,
  editable,
  disabled,
  onChanged,
}: {
  task: PipelineTask
  editable: boolean
  /** Held while the edit form has unsaved work: the returned row re-keys it. */
  disabled?: boolean
  onChanged: (updated: PipelineTask) => void
}) {
  const { busy, formError: error, setFormError: setError, run } = useSubmit()
  const value = task.language || DEFAULT_LANGUAGE
  // Not detected yet: shown as what it is treated as, with the reason on hover.
  const hint = task.language ? undefined : 'Not detected yet; treated as English'

  function choose(code: string) {
    if (code === task.language && task.language_pinned) return
    void run('Could not set the language', async () => {
      onChanged(await put<PipelineTask>(`/api/tasks/${task.id}`, { language: code }))
    })
  }

  return (
    <InlineSelect
      label="Language"
      testId="task-language"
      shown={languageName(value)}
      title={hint}
      // Undetected: a held placeholder reads as English, so picking English (what it reads as) still pins it.
      value={task.language ?? ''}
      options={[
        ...(task.language ? [] : [{ value: '', label: languageName(DEFAULT_LANGUAGE), disabled: true }]),
        ...languageOptions(value).map(o => ({ value: o.code, label: o.name })),
      ]}
      width={grid(33)}
      onChange={choose}
      inField
      editable={editable}
      disabled={disabled}
      busy={busy}
      error={error}
      onDismissError={() => setError('')}
    />
  )
}
