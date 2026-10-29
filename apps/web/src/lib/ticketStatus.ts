/* A support ticket's status and its word: here, not in `lib/support.ts`, because the inbox (`components/inbox/`) draws
 * a ticket notification's status and ships in the community edition, whose tree leaves the support modules out
 * (`support_assistant`, packages/editions/features.toml). `lib/support.ts` re-exports both. */

export type TicketStatus = 'open' | 'pending' | 'solved' | 'closed'

export const STATUS_LABEL: Record<TicketStatus, string> = { open: 'Open', pending: 'Pending', solved: 'Solved', closed: 'Closed' }
