import type { JSX } from 'react'
import { apiDetail, isNotFound } from '../../api'
import Breadcrumb, { type BreadcrumbItem } from '../../components/Breadcrumb'
import DetailStatus from './DetailStatus'

/** A run page before its run (`DetailStatus`), shared by the automation and workflow run pages: the trail ends in
 *  "Run", and not-found and the load error read alike. */
export function RunDetailStatus({ crumbs, query }: { crumbs: BreadcrumbItem[]; query: { isPending: boolean; error: unknown } }): JSX.Element {
  return (
    <DetailStatus
      trail={<Breadcrumb items={[...crumbs, { label: 'Run' }]} />}
      loading={query.isPending}
      notFound={isNotFound(query.error) && 'Run not found'}
      error={apiDetail(query.error, 'Couldn’t load this run.')}
    />
  )
}
