/* Visually hidden, not `display: none`, so the element keeps its accessible name (a status dot's label, a page
   `<h1>` the layout does not draw). MUI's own block (`@mui/utils`): a 1px box clipped
   away, its lengths as strings — a bare 0–1 `sx` number reads as a FRACTION (`width: 100%`), which once caused
   page scroll. */
export { visuallyHidden as VISUALLY_HIDDEN } from '@mui/utils'
