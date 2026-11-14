import { Link } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { useAuth, useCan } from '../auth'
import { ADD_NAME, isNameless } from '../lib/memberLabel'
import { FONT_WEIGHT, SIZE } from '../theme'
import { PERMISSION_KEYS } from '../types'
import InterstitialPanel from '../ui/InterstitialPanel'

/* The capability layer's one terminal destination: where every route guard sends a user lacking the
 * route's feature — the whole console for a zero-grant user.
 * A destination, not a dead end: it says what happened, what to do, and which account (an admin needs the
 * address to grant against), with its own Sign out (at <=480px the rail's lives in the "More" sheet).
 * Two messages: of eleven guards only one means "holds nothing"; the others mean "not THIS page". The copy
 * branches on whether the account holds any grant, so each is true for its reader.
 * No guard wraps this route (`RequireFeature`'s redirect chain ends here); there it fetches nothing.
 */
export default function NoAccess() {
  const { admin, logout } = useAuth()
  const can = useCan()
  // `can`, not `permissions.length`: it knows a superadmin holds everything. Arrow, not `some(can)`: a
  // second parameter on `can` would receive the array index (the `.map(parseInt)` footgun).
  const holdsSomething = PERMISSION_KEYS.some(key => can(key))
  // A nameless account's profile button names the fix ("Add your name"); no line of prose argues for it.
  const nameless = admin ? isNameless(admin) : false
  return (
    <InterstitialPanel
      testId="no-access"
      title={holdsSomething ? 'No access to that page' : 'No access'}
      body={holdsSomething
        ? 'Ask an administrator for access. They need the address below.'
        : 'Nothing is granted to this account yet. Ask an administrator for access. They need the '
          + 'address below.'}
    >
        {admin?.email && (
          // The address on its own line: an email has no break points and split mid-domain inline at 375px.
          // `anywhere` remains the floor for longer addresses.
          <Box sx={{ marginTop: 0.5 }}>
            <Typography variant="caption" component="div" sx={{ color: 'text.secondary' }}>
              Signed in as
            </Typography>
            <Typography
              variant="body2"
              component="div"
              sx={{ fontWeight: FONT_WEIGHT.medium, overflowWrap: 'anywhere' }}
            >
              {admin.email}
            </Typography>
          </Box>
        )}
        {/* The terminal page is the one place a zero-grant account finds Profile and Sign out. */}
        <Box sx={{ display: 'flex', justifyContent: 'flex-start', gap: 1, marginTop: 0.5 }}>
          {/* Bullet 1's carve-out must reach a zero-grant account too; its empty rail has no Profile entry
              (Sidebar.tsx's footer button is not enough). */}
          <Button
            variant="outlined"
            component={Link}
            to="/profile"
            data-testid="no-access-profile-btn"
            sx={{ minHeight: SIZE.tapTarget }}
          >
            {nameless ? ADD_NAME : 'Edit your profile'}
          </Button>
          <Button
            variant="outlined"
            onClick={() => void logout()}
            data-testid="no-access-signout-btn"
            // The rail's sign-out is desktop-footer or More-sheet only; this one is always on screen.
            sx={{ minHeight: SIZE.tapTarget }}
          >
            Sign out
          </Button>
        </Box>
    </InterstitialPanel>
  )
}
