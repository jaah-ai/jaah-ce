/** The `location.state` key a refused template start rides under on its way to the task page: the Start dialog
 *  (`pages/templates/StartTemplateDialog`) writes it, `useStartRefusedToast` reads it. Its own module so the dialog
 *  imports a string, not the task page's toast. */
export const START_REFUSED_STATE = 'startRefused'
