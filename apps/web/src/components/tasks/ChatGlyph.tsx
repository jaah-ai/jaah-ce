import { Icon } from '../../ui/icons'

/* The chat mark: an inline `<svg>` on `currentColor`, not the `💬` character. `U+1F4AC` is
 * `Emoji_Presentation=Yes` and paints in the emoji font's colours, ignoring `color` (it broke the create
 * row's blue/grey scheme and the outlined New Chat button). `U+FE0E` is unreliable across platforms and
 * `U+1F5E8` is missing from enough font stacks to render as tofu. Row-action emoji in overflow menus
 * are fine: nothing there depends on glyph colour.
 * Sized in `em` so it tracks each host's type size. Always `aria-hidden`: every host control is named. */
export default function ChatGlyph({ size = '1em' }: { size?: number | string }) {
  return (
    <Icon size={size} testId="chat-glyph" sx={{ display: 'block', flex: '0 0 auto' }}>
      {/* Balloon outline, tail bottom-left. Stroked, not filled: dots knocked out of a solid shape would show
      whatever is behind the button. `strokeLinejoin` stops the tail spiking at small sizes. */}
      <path
        d="M3 2h10a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2H8l-3 3v-3H3a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
      {/* Three dots: "a conversation", echoed by the busy state's "…". */}
      <circle cx="5" cy="6.5" r="1" fill="currentColor" />
      <circle cx="8" cy="6.5" r="1" fill="currentColor" />
      <circle cx="11" cy="6.5" r="1" fill="currentColor" />
    </Icon>
  )
}
