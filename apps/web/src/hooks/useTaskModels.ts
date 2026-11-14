import { useQuery } from '@tanstack/react-query'
import { get } from '../api'

/* The model vocabulary lives ONLY in `apps/api/app/model_catalog.py`, projected as `GET /api/tasks/models`;
 * no model name is spelled here — a typo'd id is UNROUTABLE and fails hours later on an unwatched runner. */
export type TaskModelChoice = { value: string, label: string, description: string }

export const TASK_MODELS_KEY = ['task-models'] as const
export const listTaskModels = () => get<TaskModelChoice[]>('/api/tasks/models')

/** The catalogue under a query provider; a surface mounted without one reads `cachedRead(TASK_MODELS_KEY, listTaskModels, …)`. */
export const useTaskModels = (enabled = true) =>
  useQuery({ queryKey: TASK_MODELS_KEY, queryFn: listTaskModels, staleTime: Infinity, enabled })
