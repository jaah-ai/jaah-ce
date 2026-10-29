// FIRST, before anything can make a request: a Stripe redirect's client secret leaves the address bar.
import './lib/scrubRedirectSecret'
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
import { queryClient, queryPersister, shouldPersistQuery } from './queryClient'
import { theme } from './theme'
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
              sx={{
                position: 'absolute',
                width: '1px',
                height: '1px',
                padding: 0,
                margin: '-1px',
                border: 0,
                overflow: 'hidden',
                clipPath: 'inset(50%)',
                whiteSpace: 'nowrap',
              }}
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
