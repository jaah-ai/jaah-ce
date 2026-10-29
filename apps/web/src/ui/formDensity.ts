/* The console's form density `sx` fragments: the phone touch floor and textarea resize rule.
 * Consumers are stated per export.
 * Known near-duplicate, deliberately left standing: `FIELD_SX` here and `fieldTapTargetSx` in
 * `ui/listStyles.ts` apply the same rules at different breakpoints (480px here, `down('sm')` = 600px
 * there). Both are load-bearing for geometry specs; merging them moves a floor onto ~540px screens and
 * needs its own verification pass, not a drive-by.
 */

// Consumers: `components/tasks/TaskEditForm.tsx`, `pages/ProjectEditPage.tsx`.
export const BUTTON_SX = {
  '@media (max-width:480px)': { minHeight: 44 },
  /* MUI marks a disabled button `pointer-events: none`; a native disabled button stays hit-testable.
  Reachability oracles hit-test with `elementFromPoint` to prove the mobile nav does not cover an action,
  and `none` makes the probe fall through to the container. `auto` restores native semantics; the button
  is still `disabled`, so no click fires. */
  '&.Mui-disabled': { pointerEvents: 'auto' },
} as const

// `resize: vertical` at desktop, none at <=480px (the drag grip is unusable by fingertip).
// Consumers: `components/tasks/TaskEditForm.tsx`, `components/tasks/GateForm.tsx`, `pages/ProjectEditPage.tsx`.
export const TEXTAREA_RESIZE_SX = {
  '& textarea': { resize: 'vertical' },
  '@media (max-width:480px)': { '& textarea': { resize: 'none' } },
} as const

// A record form's column: a gap under the tab strip, 640px at most (a 1440px-wide form reads as unfinished).
// Consumers: `pages/nodes/nodeLinkStore/NodeLinkEditPage.tsx`, `pages/settings/apiKeys/ApiKeyDetailPage.tsx`.
export const FORM_SX = { display: 'flex', flexDirection: 'column', gap: 2, pt: 2, maxWidth: 640 } as const

// A token field's `slotProps` (an id, a path, a CIDR): no spell-check squiggle, no autofill.
// Consumers: `pages/nodes/nodeLinkStore/{NodeLinkEditPage,StorageSourceFields}.tsx`,
// `pages/settings/apiKeys/ApiKeyDetailPage.tsx`, `pages/settings/nodelinkLocations/NodeLinkLocationsSection.tsx`.
export const TEXT_INPUT = { htmlInput: { spellCheck: false, autoComplete: 'off' } }

// The 44px touch floor at <=480px. MUI's `size="small"` input is ~32px. The floor goes on the inner
// input as well as the root: the geometry specs measure the `<input>`/`<textarea>`.
// Consumers: `components/tasks/{TaskEditForm,GateForm}.tsx`; others use `ui/listStyles`'s `fieldTapTargetSx`.
export const FIELD_SX = {
  '@media (max-width:480px)': {
    '& .MuiInputBase-root': { minHeight: 44 },
    '& .MuiInputBase-input': { minHeight: 44, boxSizing: 'border-box' },
  },
} as const
