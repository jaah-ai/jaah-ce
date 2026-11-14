// FIRST, before anything can make a request: a Stripe redirect's client secret leaves the address bar.
import './lib/scrubRedirectSecret'
// Before any request: this browser's own signed-in rate budget at the WAF.
import './lib/browserBudget'
// Before any chunk is imported: a chunk stuck on a dead connection is fetched again under a new address.
import './lib/chunkImport'
import React from 'react'
import ReactDOM from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import { PersistQueryClientProvider } from '@tanstack/react-query-persist-client'
import { StyledEngineProvider, ThemeProvider } from '@mui/material/styles'
import CssBaseline from '@mui/material/CssBaseline'
import Typography from '@mui/material/Typography'
import App from './App'
import ErrorBoundary from './components/ErrorBoundary'
import { applyLogoFavicon } from './components/logoTheme'
import { AuthProvider } from './auth'
import { get } from './api'
import { installClientTelemetry } from './lib/clientTelemetry'
import { installErrorTracking } from './lib/errorTracking'
import { installLoadTiming } from './lib/loadTiming'
import { queryClient, queryPersister, shouldPersistQuery } from './queryClient'
import { theme } from './theme'
import { VISUALLY_HIDDEN } from './ui/visuallyHidden'
import { APP_VERSION } from './version'
import './styles.css'

/* A DATA router, so a page with unsaved edits can hold the browser's Back (`hooks/useLeaveGuard.ts`, the only
reason): one splat route renders the shell, whose own `<Routes>` stay the route table. No loaders or actions. */
const router = createBrowserRouter(
  [
    {
      path: '*',
      element: (
        /* A shell-level render error gets the console's own notice, not react-router's developer page. */
        <ErrorBoundary label="the console">
          <AuthProvider>
            <App />
            {/* Style oracle for the task-detail-fit / node-detail-fit e2e specs, which read the computed font size
            of `text-xs` (the theme is unreachable from Playwright). Visually hidden but NOT `display:none`, which
            has no computed font size. */}
            <Typography
              variant="caption"
              data-testid="caption-probe"
              aria-hidden
              sx={VISUALLY_HIDDEN}
            />
          </AuthProvider>
        </ErrorBoundary>
      ),
    },
  ],
  {
    future: {
      v7_relativeSplatPath: true,
      v7_fetcherPersist: true,
      v7_normalizeFormMethod: true,
      v7_partialHydration: true,
      v7_skipActionErrorRevalidation: true,
    },
  },
)

applyLogoFavicon()
// Uncaught errors and rejections, reported only for a flagged session (`lib/clientTelemetry.ts`).
installClientTelemetry(window)
// Sentry, off until the operator sets a console DSN (`lib/errorTracking.ts`). One public-settings read per page
// load, under the login page's own query key; errors before it answers are not reported.
queryClient
  .fetchQuery({
    queryKey: ['public-settings'],
    queryFn: () => get<{ error_tracking_dsn_web?: string }>('/api/public/settings'),
  })
  .then(s => installErrorTracking(s?.error_tracking_dsn_web))
  .catch(() => undefined)
// Slow page loads and route changes, likewise reported only for a flagged session (`lib/loadTiming.ts`).
installLoadTiming(window, listener => router.subscribe(state => listener(state.location.pathname)))

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {/* injectFirst puts Emotion's <style> BEFORE styles.css, so MUI defaults do not outrank the survivor
    stylesheet (each surviving rule there carries its reason). */}
    <StyledEngineProvider injectFirst>
      {/* `colorSchemeNode={null}` is load-bearing: otherwise CssVarsProvider writes `data-theme` on <html>
      (default mode 'system', so dark OS overrides a saved LIGHT preference). `auth.tsx` is the sole writer of
      that attribute. `defaultMode="light"` matches `defaultColorScheme`. The [data-theme] variable blocks are
      static, so portalled surfaces still follow the attribute. MUI's init-color-scheme script and hook are
      deliberately unused. */}
      <ThemeProvider theme={theme} colorSchemeNode={null} defaultMode="light">
        {/* Largely inert while styles.css ships its own resets; mounted now so the ordering is settled. */}
        <CssBaseline />
        <PersistQueryClientProvider
          client={queryClient}
          // buster: APP_VERSION discards the persisted cache across a deploy (new response shapes may not match
          // an old tab's cache). dehydrateOptions: the exceptions to "persist everything", single-homed in
          // queryClient.ts; it only subtracts from react-query's default.
          persistOptions={{
            persister: queryPersister,
            maxAge: 24 * 60 * 60_000,
            buster: APP_VERSION,
            dehydrateOptions: { shouldDehydrateQuery: shouldPersistQuery },
          }}
        >
          <RouterProvider router={router} future={{ v7_startTransition: true }} />
        </PersistQueryClientProvider>
      </ThemeProvider>
    </StyledEngineProvider>
  </React.StrictMode>,
)
