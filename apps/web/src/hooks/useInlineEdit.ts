import { useState } from 'react'
import { useMutation, useQuery, useQueryClient, type QueryKey } from '@tanstack/react-query'
import { apiDetail } from '../api'

/** A read shown with inline edits: one query, one write whose answer replaces the cache and closes the edit,
 *  and the open edit itself (which row, its draft). `error` is the failed write's, else the failed read's. */
export function useInlineEdit<T, Body>({ queryKey, load, write, loadError, saveError }: {
  queryKey: QueryKey
  load: () => Promise<T>
  write: (body: Body) => Promise<T>
  loadError: string
  saveError: string
}) {
  const client = useQueryClient()
  const query = useQuery({ queryKey, queryFn: load })
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const mutation = useMutation({
    mutationFn: write,
    onSuccess: next => {
      client.setQueryData(queryKey, next)
      setEditing(null)
    },
  })
  return {
    data: query.data,
    loading: query.isPending,
    error: mutation.isError ? apiDetail(mutation.error, saveError) : query.isError ? apiDetail(query.error, loadError) : '',
    busy: mutation.isPending,
    editing,
    draft,
    setDraft,
    startEdit: (key: string, value: string) => {
      mutation.reset()
      setEditing(key)
      setDraft(value)
    },
    cancel: () => setEditing(null),
    save: (body: Body) => mutation.mutate(body),
  }
}
