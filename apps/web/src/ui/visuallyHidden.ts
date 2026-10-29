/* Visually hidden, not `display: none`, so the element keeps its accessible name (a status dot's label, a page
   `<h1>` the layout does not draw). Inlined rather than `@mui/utils`'s: that is not a declared dependency.
   `'1px'`, NOT `1`: MUI reads a bare 0–1 number as a FRACTION (`width: 100%`), which once caused page scroll. */
export const VISUALLY_HIDDEN = {
  position: 'absolute',
  width: '1px',
  height: '1px',
  padding: 0,
  margin: '-1px',
  border: 0,
  overflow: 'hidden',
  clipPath: 'inset(50%)',
  whiteSpace: 'nowrap',
} as const
