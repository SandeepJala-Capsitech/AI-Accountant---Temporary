// What a waiting suggestion would change, which suggestions make the same change, and which document a card can
// show beside it: the rules behind the AI review page's suggestions. Kept apart from the network so node --test can
// run it.
import type { Decision } from './chat.ts'
import { changedFields } from './chat.ts'
import type { Upload } from './clients'

const none = (value: string) => (value === '—' ? 'none' : value)

// One line for each field a suggestion changes: "Date: none → 2 Sep 2026", or "Row 12 · Account: 7500 → 7400" when
// it changes more than one row. At most `most` lines; `more` is how many were left out.
export function changeLines(d: Decision, most = 4): { lines: string[]; more: number } {
  const changes = changedFields(d)
  const several = new Set(changes.map(c => c.row)).size > 1
  const lines = changes.map(c => `${several ? `Row ${c.row} · ` : ''}${c.field}: ${none(c.before)} → ${none(c.after)}`)
  return { lines: lines.slice(0, most), more: Math.max(0, lines.length - most) }
}

// Suggestions that make the same change, so they can be applied together: the same kind of change to the same new
// values, on different rows. Only groups of two or more, in the order their first suggestion is listed. A suggestion
// that changes no field (it keeps one of two records, say) is its own decision and joins none.
export interface FixGroup { key: string; label: string; suggestions: Decision[]; rows: number }
export function sameFixGroups(suggestions: Decision[]): FixGroup[] {
  const groups = new Map<string, { to: string[]; suggestions: Decision[] }>()
  for (const d of suggestions) {
    const to = [...new Set(changedFields(d).map(c => `${c.field}=${none(c.after)}`))].sort()
    if (!to.length) continue
    const key = `${d.action}|${to.join('|')}`
    const group = groups.get(key) ?? { to, suggestions: [] }
    group.suggestions.push(d)
    groups.set(key, group)
  }
  return [...groups.entries()].filter(([, g]) => g.suggestions.length > 1).map(([key, g]) => {
    const rows = new Set(g.suggestions.flatMap(d => d.row_ids)).size
    return { key, label: `${g.to.map(t => t.replace('=', ' → ')).join(', ')} on ${rows} rows`, suggestions: g.suggestions, rows }
  })
}

// Whether a browser can show the file in the page, by its name: a PDF or an image. The API sends only these for
// showing (SHOWN_IN_PAGE in server.py), whatever is asked.
const SHOWN = /\.(pdf|png|jpe?g|webp|gif)$/i
export const canShow = (name: string): boolean => SHOWN.test(name)
export const isPdf = (name: string): boolean => /\.pdf$/i.test(name)

// The upload a row's document can be viewed from; none for a row typed in by hand.
export function documentOf(row: { upload_id: number }, uploads: Upload[]): Upload | null {
  const upload = uploads.find(u => u.id === row.upload_id)
  return upload && upload.kind !== 'manual' ? upload : null
}

// The original file of an upload: as a download, or (show) for the page to show in place.
export const fileHref = (clientId: number, uploadId: number, show = false): string =>
  `/api/clients/${clientId}/uploads/${uploadId}/file${show ? '?show=1' : ''}`
