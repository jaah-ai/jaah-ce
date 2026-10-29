/* A link that opens My Profile's Git tab with its Add Git account form already open: `/profile` honours `?add=1`
   (`pages/ProfilePage.tsx`) and drops it from the URL once read, so Back and a reload do not reopen the form. */
export const GIT_ADD_PARAM = 'add'
export const GIT_ADD_PATH = `/profile?tab=git-accounts&${GIT_ADD_PARAM}=1`
