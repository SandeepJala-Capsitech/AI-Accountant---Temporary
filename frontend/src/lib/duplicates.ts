// Finds what was entered twice: the same file again, or the same payment from two inputs (say a
// receipt and the bank line for it). Nothing is removed: the UI skips a repeated file and flags
// rows that may repeat each other, and a person decides.

// SHA-256 of a file's bytes as hex: the same content gives the same fingerprint, whatever its name.
// null when the browser cannot hash (crypto.subtle needs a secure context such as localhost).
export async function fingerprint(file: File): Promise<string | null> {
  try {
    const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
    return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('')
  } catch {
    return null
  }
}

export const DUPLICATE_WINDOW_DAYS = 3   // card payments reach the bank a day or two after the receipt
const DAY_MS = 86_400_000

export interface PaymentRow {
  sourceId: number         // the file, paste or manual entry the row came from
  gross: string
  direction: string
  date: string | null      // ISO YYYY-MM-DD
}

// For each row, the index of an earlier row from another input that it may duplicate: the same
// amount and direction, dated within DUPLICATE_WINDOW_DAYS (or both undated); otherwise null.
// Rows of one input are not compared: two equal payments on one statement are usually real.
export function possibleDuplicates(rows: PaymentRow[]): (number | null)[] {
  return rows.map((row, i) => {
    for (let j = 0; j < i; j++) {
      const other = rows[j]
      if (other.sourceId === row.sourceId || other.direction !== row.direction
          || Number(other.gross) !== Number(row.gross)) continue
      if (row.date === null || other.date === null) {
        if (row.date === other.date) return j
        continue
      }
      if (Math.abs(Date.parse(row.date) - Date.parse(other.date)) <= DUPLICATE_WINDOW_DAYS * DAY_MS) return j
    }
    return null
  })
}
