import { useQuery } from '@tanstack/react-query'
import { apiDetail } from '../../../api'
import { listActions, type CatalogAction } from '../../../components/access/api'
import { gridRows, type GridRow } from './keyPermissions'

/* The permission grid's rows, from the served catalog (a module constant server-side: read once per visit). */
export function useGridRows(): { rows: GridRow[] | null; error: string } {
  const query = useQuery({
    queryKey: ['permission-catalog'],
    staleTime: Infinity,
    queryFn: async () => {
      const catalog: unknown = await listActions()
      if (!Array.isArray(catalog)) throw new Error('The permission list came back in an unexpected shape.')
      return catalog as CatalogAction[]
    },
  })
  return {
    rows: query.data ? gridRows(query.data) : null,
    error: query.error ? apiDetail(query.error, 'Could not load the permission list.') : '',
  }
}
