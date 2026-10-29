import Placeholder from '@tiptap/extension-placeholder'
import TaskItem from '@tiptap/extension-task-item'
import TaskList from '@tiptap/extension-task-list'
import { Extension, type AnyExtension } from '@tiptap/core'
import { Markdown } from 'tiptap-markdown'
import StarterKit from '@tiptap/starter-kit'
import { Plugin } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import { AttachmentImage } from './attachmentImage'
import { type AttachmentResolver, isAttachmentSrc } from './attachmentResolver'

// The ONE schema shared by RichTextEditor and RichTextRenderer (AC-9): identical node and mark sets keep the
// round-trip faithful, and no raw-HTML node is ever registered.
export function buildExtensions({
  editable,
  resolveAttachmentSrc,
  placeholder,
  linkify = true,
  untrusted = false,
  lineBreaks = false,
}: {
  editable: boolean
  resolveAttachmentSrc?: AttachmentResolver
  /* Autolink bare URL-like text (default on). Off for documentation readers: `.md` is a real TLD, so
     `CLAUDE.md` would become a dead link. Explicit `[text](target)` links are unaffected. */
  linkify?: boolean
  /* Content written by someone other than the viewer (a tenant's ticket, read by an operator): see
     `UntrustedContent`. */
  untrusted?: boolean
  /* Enter starts a new LINE, not a paragraph (`EnterLineBreak`): for a plain-text template, where a blank line
     between every typed line is noise. */
  lineBreaks?: boolean
  /* A string, or an ACCESSOR re-read on every decoration pass, which is how a LIVE editor's placeholder
     changes: `useEditor` builds once and `configure` captures its options (mutating them does nothing).
     See `RichTextEditor`'s effect. */
  placeholder?: string | (() => string)
}): AnyExtension[] {
  return [
    StarterKit.configure({
      link: {
        /* No `protocols`: it only ADDS schemes, and http/https/mailto are already linkifyjs's and `isAllowedUri`'s
           built-ins, which refuse unsafe ones (AC-9). Each editor's Link `onCreate` registers them into the one
           global linkifyjs, so once any editor autolinked, every later one warned "already initialized". */
        openOnClick: !editable,
        HTMLAttributes: { rel: 'noopener noreferrer', target: '_blank' },
      },
      // Image is replaced by AttachmentImage below (StarterKit v3 doesn't bundle image).
    }),
    AttachmentImage.configure({ inline: false, allowBase64: false, resolveAttachmentSrc }),
    TaskList,
    TaskItem.configure({ nested: true }),
    Markdown.configure({
      // Explicit (tiptap-markdown defaults `html: true`): raw HTML is escaped to text by markdown-it —
      // the load-bearing half of AC-9's hostile-paste sanitization.
      html: false,
      linkify,
      transformPastedText: true,
      transformCopiedText: true,
    }),
    AutolinkStopsAtQuote,
    NoAutolinkFilename,
    ...(untrusted ? [UntrustedContent] : []),
    ...(editable ? [ModifierClickLink] : []),
    ...(editable && lineBreaks ? [EnterLineBreak] : []),
    ...(editable && placeholder ? [Placeholder.configure({ placeholder })] : []),
  ]
}

/* A bare URL ends before a `"`: neither linkifier does on its own, so `[{"url":"https://host/p/","name":"a b"}]`
   linked `https://host/p/","name":"a` (linkify-it allows a quoted run inside a path; linkifyjs, behind the Link
   mark's typed autolink, lets a URL run through quotes). Parsed markdown: markdown-it's linkify matches are cut
   at the quote. Typed text: an autolinked mark (its text IS its href) is trimmed right after Link adds it.
   An explicit `[text](url)` link is neither: its href is percent-encoded, so never equals a text holding `"`. */
const AutolinkStopsAtQuote = Extension.create({
  name: 'autolinkStopsAtQuote',
  addStorage: () => linkifyStorage(cutAtQuote),
  addProseMirrorPlugins() {
    return [new Plugin({
      appendTransaction(transactions, _oldState, state) {
        if (!transactions.some(tr => tr.docChanged)) return null
        const linkType = state.schema.marks.link
        const tr = state.tr
        state.doc.descendants((node, pos) => {
          const link = node.isText ? linkType.isInSet(node.marks) : undefined
          const text = node.text ?? ''
          const cut = text.indexOf('"')
          if (!link || cut < 0 || ![text, `http://${text}`].includes(link.attrs.href)) return
          const href = link.attrs.href as string
          tr.removeMark(pos + cut, pos + text.length, linkType)
          if (cut > 0) tr.addMark(pos, pos + cut, linkType.create({ ...link.attrs, href: href.slice(0, href.length - (text.length - cut)) }))
        })
        return tr.docChanged ? tr : null
      },
    })]
  },
})

/* A bare filename is not a URL: `README.md`, `setup.py`, `run.sh` end in real TLDs (Moldova, Paraguay, St Helena),
   so both linkifiers turned them into `http://README.md`. A match with no scheme, path or `@` that ends in a
   code-file extension stays text — parsed (markdown-it's matches are dropped) and typed (Link's autolinked mark,
   whose href is `http://` + its text, is removed). An explicit `[text](url)` or `https://README.md` still links. */
const NoAutolinkFilename = Extension.create({
  name: 'noAutolinkFilename',
  addStorage: () => linkifyStorage(found => (isBareFilename(found.raw) ? null : found)),
  addProseMirrorPlugins() {
    return [new Plugin({
      appendTransaction(transactions, _oldState, state) {
        if (!transactions.some(tr => tr.docChanged)) return null
        const linkType = state.schema.marks.link
        const tr = state.tr
        state.doc.descendants((node, pos) => {
          const link = node.isText ? linkType.isInSet(node.marks) : undefined
          const text = node.text ?? ''
          if (link && link.attrs.href === `http://${text}` && isBareFilename(text)) tr.removeMark(pos, pos + text.length, linkType)
        })
        return tr.docChanged ? tr : null
      },
    })]
  },
})

const CODE_FILE_EXTENSION = /\.(md|mdx|py|sh|rs|ts|tsx|js|jsx|mjs|go|rb|pl|php|java|kt|swift|c|h|cpp|cs|sql|json|yaml|yml|toml|ini|cfg|env|lock|txt|log|csv|xml|html|css|scss)$/i

export function isBareFilename(raw: string): boolean {
  return !/[/:@]/.test(raw) && CODE_FILE_EXTENSION.test(raw)
}

// The slice of markdown-it's linkify-it instance (and its match) that the cut reads and replaces.
type LinkifyMatch = { raw: string, text: string, url: string, lastIndex: number }
type Linkify = { match(text: string): LinkifyMatch[] | null, matchAtStart(text: string): LinkifyMatch | null }

/* The `tiptap-markdown` storage that rewrites markdown-it's linkify matches through `edit` — a match kept as
   given, changed, or dropped (null). Both linkify-side rules above are one of these. `match` answers an empty
   list, never null, for all-dropped: markdown-it reads `.length` once `test()` said yes. */
function linkifyStorage(edit: (found: LinkifyMatch) => LinkifyMatch | null) {
  return {
    markdown: {
      parse: {
        setup(md: { linkify: Linkify }) {
          const { linkify } = md
          const match = linkify.match.bind(linkify)
          const matchAtStart = linkify.matchAtStart.bind(linkify)
          linkify.match = text => match(text)?.flatMap(m => edit(m) ?? []) ?? null
          linkify.matchAtStart = text => {
            const found = matchAtStart(text)
            return found && edit(found)
          }
        },
      },
    },
  }
}

function cutAtQuote(found: LinkifyMatch): LinkifyMatch {
  const cut = found.raw.indexOf('"')
  if (cut < 0) return found
  const drop = found.raw.length - cut
  return {
    ...found,
    raw: found.raw.slice(0, cut),
    text: found.text.slice(0, -drop),
    url: found.url.slice(0, -drop),
    lastIndex: found.lastIndex - drop,
  }
}

const OPEN_LINK_HINT = '⌘/Ctrl+click to open'

/* While editing, a plain click on a link places the caret in it (so its text stays editable); Ctrl/⌘+click opens it
   in a new tab, as in Google Docs. The href is the rendered one, already filtered by the Link mark's `isAllowedUri`.
   The hover hint is a decoration, never a link attribute, so it stays out of the markdown and the clipboard. */
const ModifierClickLink = Extension.create({
  name: 'modifierClickLink',
  addProseMirrorPlugins() {
    return [new Plugin({
      props: {
        decorations(state) {
          const hints: Decoration[] = []
          state.doc.descendants((node, pos) => {
            if (node.isText && node.marks.some(m => m.type.name === 'link')) {
              hints.push(Decoration.inline(pos, pos + node.nodeSize, { title: OPEN_LINK_HINT }))
            }
          })
          return DecorationSet.create(state.doc, hints)
        },
        handleDOMEvents: {
          click(_view, event) {
            if (!(event.metaKey || event.ctrlKey) || event.button !== 0) return false
            const href = (event.target as HTMLElement | null)?.closest('a')?.getAttribute('href')
            if (!href) return false
            event.preventDefault()
            window.open(href, '_blank', 'noopener,noreferrer')
            return true
          },
        },
      },
    })]
  },
})

/* Defangs untrusted markdown at parse time, on tiptap-markdown's rendered HTML (an inert `DOMParser`
   document, so nothing in it has loaded yet), before ProseMirror sees it:
   - an image whose src is not `attachment:` becomes its alt text — an external URL would load in the
     reader's browser (a tracking pixel, an IP leak);
   - a link becomes plain text showing its URL (`text (url)`, or the URL once when they match) — an anchor
     whose text hides its target is a phishing surface. */
const UntrustedContent = Extension.create({
  name: 'untrustedContent',
  addStorage() {
    return {
      markdown: {
        parse: {
          updateDOM(element: HTMLElement) {
            const doc = element.ownerDocument
            for (const img of Array.from(element.querySelectorAll('img'))) {
              if (!isAttachmentSrc(img.getAttribute('src') ?? '')) img.replaceWith(doc.createTextNode(img.getAttribute('alt') ?? ''))
            }
            for (const a of Array.from(element.querySelectorAll('a'))) {
              const href = a.getAttribute('href') ?? ''
              const text = a.textContent ?? ''
              if (href && href !== text && `mailto:${text}` !== href) a.append(doc.createTextNode(` (${href})`))
              a.replaceWith(...Array.from(a.childNodes))
            }
          },
        },
      },
    }
  },
})

/* Enter in a plain paragraph inserts a line break (markdown `\` + newline: one line under the last, no blank line
   between), and a second Enter straight after one (or in an empty line) opens a new paragraph, as a blank line
   does. Lists, headings
   and code blocks keep their own Enter. Ahead of StarterKit's keymaps, which would split the paragraph. */
const EnterLineBreak = Extension.create({
  name: 'enterLineBreak',
  priority: 1000,
  addKeyboardShortcuts() {
    return {
      Enter: ({ editor }) => {
        const { $from, empty } = editor.state.selection
        // An empty paragraph takes the plain Enter too: a break there would be a line holding only `\`.
        if (!empty || $from.parent.type.name !== 'paragraph' || $from.parent.content.size === 0
          || editor.isActive('listItem') || editor.isActive('taskItem')) {
          return false
        }
        const before = $from.nodeBefore
        if (before?.type.name === 'hardBreak') {
          return editor.chain().deleteRange({ from: $from.pos - before.nodeSize, to: $from.pos }).splitBlock().run()
        }
        return editor.commands.setHardBreak()
      },
    }
  },
})
