import Tasks from '../Tasks'

/* `/tasks` — THE PIPELINE PAGE: the stage strip above search and one project's work by stage, on the
 * board's rail, toolbar, rows and verbs (`pages/Tasks.tsx` with `pipeline`). Pick a project in the sidebar's
 * Projects tree to work its pipeline; with none picked, the stage totals head today's all-projects list (D7). */
export default function PipelinePage() {
  return <Tasks pipeline />
}
