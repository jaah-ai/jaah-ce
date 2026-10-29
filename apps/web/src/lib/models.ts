// A model id as a heading: "opus" → "Opus". A BARE CANONICAL FAMILY is the normal input now that the
// platform stores one; the dated `claude-<family>-<ver>` shapes are historical and still format —
// "claude-opus-4-8" → "Opus 4.8", with a trailing date segment dropped (e.g. "...-20250805"). Falls
// back to the raw id for an unexpected shape (e.g. an older dated sonnet or a future naming scheme).
export function fmtModel(id: string | null): string {
  if (!id) return '—'
  let parts = id.replace(/^claude-/, '').split('-')
  if (parts.length > 1 && /^\d{6,}$/.test(parts[parts.length - 1])) parts = parts.slice(0, -1)
  const family = parts[0]
  if (!/^[a-z]+$/.test(family)) return id
  const cap = `${family[0].toUpperCase()}${family.slice(1)}`
  const version = parts.slice(1).join('.')
  return version ? `${cap} ${version}` : cap
}

// Brief form for table cells: the family alone. A canonical "sonnet" is already it; the historical
// "claude-sonnet-4-6" reduces to it. Falls back to fmtModel for unrecognised shapes.
export function fmtModelBrief(id: string | null): string {
  if (!id) return '—'
  const parts = id.replace(/^claude-/, '').split('-')
  const family = parts[0]
  if (!/^[a-z]+$/.test(family)) return fmtModel(id)
  return family
}

// A model TIER id as its label: "high" → "High". The tier set is the server's (`app/model_catalog.py`,
// `GET /api/tasks/models`); a surface without the catalogue in hand capitalizes, which is exactly its label.
export function fmtTier(id: string): string {
  return id ? `${id[0].toUpperCase()}${id.slice(1)}` : id
}

// A priced usage key as its label. The node suffixes a model id with its pricing tier (`@peak`,
// `@long-context`; `app/provider_model_prices.py`), which is a join key, not a name: "deepseek-flash@peak"
// → "deepseek-flash · peak", "gpt-5.5@long-context" → "gpt-5.5 · long context". A bare id is unchanged.
export function fmtPriceKey(key: string): string {
  const at = key.lastIndexOf('@')
  if (at <= 0 || at === key.length - 1) return key
  return `${key.slice(0, at)} · ${key.slice(at + 1).replace(/-/g, ' ')}`
}
