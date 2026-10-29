import type { JSX, ReactNode } from 'react'
import Banner from './Banner'
import CopyableValue from './CopyableValue'
import FormDialog from './FormDialog'

/* THE ONE TIME A SECRET IS SHOWN (a trigger's on create/reveal/rotate, an API key's on create/rotate). The
 * secret lives only in the caller's transient state, passed in here; closing hands control back so the
 * caller clears it, and nothing shows it again (`app/api_keys.py` keeps only its hash). A TRUE modal:
 * neither a stray click nor Escape drops it before it is copied — the one button is the way out, and
 * pressing it is the operator saying they stored it. */
export default function SecretOnceDialog({
  secret,
  title,
  notice,
  submitLabel,
  testIdPrefix,
  valueTestId = `${testIdPrefix}-value`,
  onClose,
}: {
  /** `null` = closed. */
  secret: string | null
  title: ReactNode
  /** The warning line above the value: must say it is shown once. */
  notice: ReactNode
  /** The acknowledgement verb ("Done", "I’ve stored it"). */
  submitLabel: string
  /** Yields `-dialog`, `-warning`, `-value`, `-copy`, `-done`. */
  testIdPrefix: string
  /** Overrides the value box's id where an e2e already reads another. */
  valueTestId?: string
  onClose: () => void
}): JSX.Element {
  return (
    <FormDialog
      open={secret !== null}
      modal
      hideCancel
      title={title}
      submitLabel={submitLabel}
      testId={`${testIdPrefix}-dialog`}
      submitTestId={`${testIdPrefix}-done`}
      onSubmit={onClose}
      onClose={onClose}
    >
      <Banner severity="warning" testId={`${testIdPrefix}-warning`}>{notice}</Banner>
      <CopyableValue
        value={secret ?? ''}
        copyLabel="Copy secret"
        testId={valueTestId}
        copyTestId={`${testIdPrefix}-copy`}
        sx={{ flex: '1 1 16rem', overflowWrap: 'anywhere' }}
      />
    </FormDialog>
  )
}
