/* The axis tracks' look (`TaskAxisControls`'s `GROUP_SX`), single-homed so the compact paperclip on the phone's axes
 * line (`TaskAttachmentsField`'s `COMPACT_ATTACH_SX`) wears it too and the line reads as one set. Its
 * own module so a general attachment field does not import a form's controls. */

/** Fully round. */
export const AXIS_TRACK_RADIUS = '999px'

/** The recessed ground, in the channel form (`--mui-palette-*Channel`) so it follows the colour scheme. */
export const AXIS_TRACK_GROUND = 'rgba(var(--mui-palette-common-onBackgroundChannel, 0 0 0) / 0.05)'
