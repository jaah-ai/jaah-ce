import { listActions, type CatalogAction } from '../../../components/access/api'
import { useApiRead } from '../../../hooks/useApiRead'
import { gridRows, type GridRow } from './keyPermissions'

/* The permission grid's rows, from the served catalog (a module constant server-side: read once per visit). */
export function useGridRows(): { rows: GridRow[] | null; error: string } {
  const { data, error } = useApiRead<CatalogAction[]>(['permission-catalog'], listActions, 'Could not load the permission list.', {
    staleTime: Infinity,
    shapeError: 'The permission list came back in an unexpected shape.',
  })
  return { rows: data ? gridRows(data) : null, error }
}
