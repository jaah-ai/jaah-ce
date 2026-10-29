import { Component, type ErrorInfo, type ReactNode } from 'react'
import Alert from '@mui/material/Alert'
import AlertTitle from '@mui/material/AlertTitle'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'

// Catches page render errors so one bad payload does not white-screen the app (React only lets a class
// component do this). Wraps the routed <Outlet /> INSIDE AuthLayout, so the Sidebar survives a crash.
//
// Two signals. The KEY (`pageKey(pathname)`, single-homed in `routeGroups.ts`, on the Suspense around this
// boundary in `components/ListDetail.tsx`) is mount identity and
// must stay narrow, or every tab click remounts. `resetOn` (raw pathname) is error lifetime and must stay
// wide: a crashed tab shell renders no tab strip, and the rail's rows for one shell all point at ONE
// page (`/groups` → `/users` is a real navigation the key cannot see), so without it the crash screen
// follows the operator onto a healthy address.

interface Props {
  children: ReactNode
  // Human name of the surface being guarded, woven into the fallback copy ("the Tasks view", etc.).
  label?: string
  // Changes whenever the operator navigates (raw pathname, from App.tsx); clears a latched error.
  resetOn?: unknown
  // Replaces the stock alert, for a boundary around one panel of a page rather than the page.
  fallback?: ReactNode
}

interface State {
  error: Error | null
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidUpdate(prevProps: Props): void {
    // Guarded on `error` so a routine navigation costs no state write / subtree re-render.
    if (this.state.error && prevProps.resetOn !== this.props.resetOn) {
      this.setState({ error: null })
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // The boundary swallows the throw, so log it for devtools triage.
    console.error('ErrorBoundary caught a render error', error, info.componentStack)
  }

  private handleRetry = (): void => {
    // Retry the child render; if the cause persists the boundary catches again.
    this.setState({ error: null })
  }

  render(): ReactNode {
    const { error } = this.state
    if (!error) return this.props.children
    if (this.props.fallback !== undefined) return this.props.fallback

    const surface = this.props.label ?? 'this view'
    return (
      // Stock MUI `<Alert severity="error">`; `role="alert"` is written out because the boundary's contract
      // depends on it.
      <Alert
        severity="error"
        role="alert"
        data-testid="error-boundary"
        action={
          <Button
            color="inherit"
            size="small"
            onClick={this.handleRetry}
            // MUI's stock focus treatment on a text button is a ripple, not a visible ring.
            sx={{ '&.Mui-focusVisible': { outline: '2px solid currentColor', outlineOffset: '2px' } }}
          >
            Retry
          </Button>
        }
        sx={{ maxWidth: '32rem', mx: 'auto', my: 3, alignItems: 'center' }}
      >
        <AlertTitle sx={{ mb: 0.5 }}>Something went wrong loading {surface}.</AlertTitle>
        <Typography variant="body2">
          The rest of the console still works — navigate elsewhere, or retry below.
        </Typography>
      </Alert>
    )
  }
}
