import { type JSX, type ReactNode, useState } from 'react'
import Box from '@mui/material/Box'
import AdvancedSection from '../../../ui/AdvancedSection'
import FormBox from '../../../ui/FormBox'
import FocusSafe from './FocusSafe'
import { FONT_SIZE, FONT_WEIGHT } from '../../../theme'
import type { CustomizeSectionDef, CustomizeSectionKey } from './sections'

/* THE PROJECT PAGE'S CUSTOMIZE TAB (`/projects/:id?tab=customize`): the ordered sections of `customize/sections.tsx`,
 * each a fold (`ui/AdvancedSection`), all closed unless a link names one (`?section=`, or a retired tab's id), any
 * number open together. A section's title carries a dot and "On" while it is activated (`active`, S7).
 *
 * Two kinds of section share the tab. The leading ones whose fields the project form saves (`inForm`) sit in a form of
 * their own that submits the page's Save (`onSubmit`), with the page's Save bar (`actions`) right under them; the rest
 * (MCP, MicroVM, Live Cast) save on their own and sit outside it, since MicroVM's editors are forms themselves and a
 * form may not nest.
 *
 * Mounted with the page, as every form panel is (a hidden field still answers the Save's checks and takes its focus);
 * the self-saving bodies from the tab's first open on (`open`; kept after, so an unsaved MicroVM edit survives a tab hop)
 * and the Save bar only while it is open, so they never fetch early or double the bar behind another tab. */

function SectionLabel({ section }: { section: CustomizeSectionDef }): JSX.Element {
  return (
    <>
      {section.title}
      {section.active && (
        <Box
          component="span"
          data-testid={`customize-${section.key}-on`}
          sx={{ ml: 1, color: 'success.main', fontSize: FONT_SIZE.sm, fontWeight: FONT_WEIGHT.regular, whiteSpace: 'nowrap' }}
        >
          <Box component="span" aria-hidden="true">● </Box>On
        </Box>
      )}
    </>
  )
}

export default function CustomizeTab({ sections, open, openKey, errorKey, formId, onSubmit, notice, actions }: {
  sections: readonly CustomizeSectionDef[]
  /** The tab is the one shown. */
  open: boolean
  /** The section a link names: open on arrival. */
  openKey: CustomizeSectionKey | null
  /** The section holding the field the last Save refused: held open until the next Save. */
  errorKey: CustomizeSectionKey | null
  formId: string
  onSubmit: () => void | Promise<unknown>
  /** Above the sections (the governance read-only notice). */
  notice?: ReactNode
  /** The page's Save bar, under the form-saved sections while any shows; the host omits it while nothing changed. */
  actions?: ReactNode
}): JSX.Element {
  const [visited, setVisited] = useState(open)
  if (open && !visited) setVisited(true)
  const shown = sections.filter(s => s.shown)
  const firstOwn = shown.findIndex(s => !s.inForm)
  const inForm = firstOwn === -1 ? shown : shown.slice(0, firstOwn)
  const own = firstOwn === -1 ? [] : shown.slice(firstOwn)
  const fold = (section: CustomizeSectionDef, body: boolean) => (
    <AdvancedSection
      key={section.key}
      testId={`customize-${section.key}`}
      label={<SectionLabel section={section} />}
      defaultOpen={section.key === openKey}
      forceOpen={section.key === errorKey}
    >
      <Box sx={{ pl: { xs: 0.5, sm: 2 }, pb: 1, minWidth: 0 }}>{body && section.render()}</Box>
    </AdvancedSection>
  )
  return (
    <Box data-testid="project-customize" sx={{ pt: 1.5, minWidth: 0 }}>
      {notice}
      {inForm.length > 0 && (
        <FormBox id={formId} onSubmit={onSubmit} data-testid="project-customize-form">
          {inForm.map(s => fold(s, true))}
          {/* Inside the form: it sticks only while the sections it saves scroll, and ends under them, never below the
              self-saving ones it does not save. */}
          {open && actions && <FocusSafe>{actions}</FocusSafe>}
        </FormBox>
      )}
      {own.map(s => fold(s, visited))}
    </Box>
  )
}
