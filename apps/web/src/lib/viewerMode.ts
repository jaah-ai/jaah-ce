import { createContext, useContext } from 'react'

/* LIVE CAST'S VIEWER MODE. The live host (`live.html`, `src/live/`) mounts the
 * console's own task list and task detail inside this context; the console never provides it, so every reader is
 * `false` there and nothing changes. In `lib/`, not `src/live/`: the console's own components read it, and the
 * community edition's tree ships no viewer (`live_cast`, packages/editions/features.toml).
 *
 * The viewer sees everything a console `tasks:view` reader sees, read-only, through the same components. The live
 * entry mounts no `AuthProvider`, so `useCan()` and every `useTaskAccess` predicate answer false and the write
 * controls they gate are gone already; this flag covers only what a permission does not: the comment composer (any
 * `tasks:view` reader may post) and the vote, plus the operator's four exceptions — people (Assigned to,
 * Created by, Updated by; the API sends them null), cross-task edges (Depends on / Blocks), the compliance manifest
 * (the sealed file names people by email) and the toolbar's Hidden switch (the live API always lists hidden rows).
 * One condition at each control — never a copied component (NC-4). */
export const ViewerModeContext = createContext(false)

/** Is this page the public Live Cast viewer? */
export const useViewerMode = (): boolean => useContext(ViewerModeContext)
