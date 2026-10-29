import type { JSX, ReactNode } from 'react'
import EmptyState from '../EmptyState'
import FeedList from './FeedList'
import LoadingState from '../LoadingState'

/* Every list's body: skeleton, load failure (never an empty list), "no match" vs "nothing yet" (different facts),
   rows. `children` follow the rows only. */
export default function ListStates({
  loading, skeleton, failed, empty, noMatch, emptyTitle, emptyScoped, emptyAction, feed, children,
}: {
  loading: boolean
  /** The `LoadingState` props: `rows` for the skeleton, none for the inline spinner. */
  skeleton: Parameters<typeof LoadingState>[0]
  /** The load-failure state; `true` renders nothing here because the page's banner above already says it. */
  failed?: ReactNode
  empty: boolean
  /** Shown instead of `emptyTitle` when the filters emptied the list; falsy = they did not. */
  noMatch: ReactNode
  emptyTitle: string
  emptyScoped?: boolean
  /** The empty state's one call-to-action (usually the list's New button, which the header then drops). */
  emptyAction?: ReactNode
  feed: Parameters<typeof FeedList>[0]
  children?: ReactNode
}): JSX.Element {
  if (loading) return <LoadingState {...skeleton} />
  if (failed) return <>{failed}</>
  if (empty) return <>{noMatch || <EmptyState title={emptyTitle} scoped={emptyScoped} action={emptyAction} />}</>
  return <><FeedList {...feed} />{children}</>
}
