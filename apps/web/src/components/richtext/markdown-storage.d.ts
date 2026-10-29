import type { MarkdownStorage } from 'tiptap-markdown'

// tiptap-markdown ships `MarkdownStorage` but doesn't itself merge it into `@tiptap/core`'s `Storage`
// interface, so `editor.storage.markdown` is untyped without this augmentation.
declare module '@tiptap/core' {
  interface Storage {
    markdown: MarkdownStorage
  }
}
