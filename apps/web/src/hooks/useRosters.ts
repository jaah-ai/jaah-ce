import { type UseQueryOptions, useQuery } from '@tanstack/react-query'
import { get } from '../api'
import type { Project, Worktree } from '../types'

/* The two org-wide rosters every surface reads under ONE key each, so a page arriving from another is served from
 * cache and a write anywhere invalidates them all (`pages/ProjectCreateWizard.tsx`, `pages/ProjectEditPage.tsx`).
 * `components/ConsoleDock.tsx` only SUBSCRIBES to `WORKTREES_KEY`; `components/WorktreePool.tsx` owns its refetch.
 * Each caller keeps its own `enabled` (`GET /api/worktrees` is `worktrees:view`) and polling; variants under another
 * key (`credentialForm.tsx`'s act-as read, `GhaRunnersTab.tsx`) stay their own. */

export const PROJECTS_KEY = ['projects'] as const
export const WORKTREES_KEY = ['worktrees'] as const

export const listProjects = () => get<Project[]>('/api/projects')
export const listWorktrees = () => get<Worktree[]>('/api/worktrees')

type RosterOptions<T> = Omit<UseQueryOptions<T[]>, 'queryKey' | 'queryFn'>

export const useProjects = (options?: RosterOptions<Project>) =>
  useQuery({ queryKey: PROJECTS_KEY, queryFn: listProjects, ...options })

export const useWorktrees = (options?: RosterOptions<Worktree>) =>
  useQuery({ queryKey: WORKTREES_KEY, queryFn: listWorktrees, ...options })
