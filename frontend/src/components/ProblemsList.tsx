'use client'

import Link from 'next/link'
import type { Decision } from '@/lib/chat'
import type { ProblemGroup } from '@/lib/problems'
import { problemCount } from '@/lib/problems'

// What a waiting suggestion would do, in a line. An account change names the account it moves the row to (its
// reason repeats what the problem already says); any other says its reason.
const suggested = (d: Decision, rowId: number): string => {
  const to = d.action === 'change_account' ? d.after[String(rowId)]?.account_code : undefined
  return to ? `Suggested: change the account to ${to}.` : `Suggested: ${d.reason}`
}

// Every open problem of the year in one place, by kind: what the row is, what the check says, a link to the row,
// and, where a change is waiting for the person, what it would do and Apply.
export default function ProblemsList({ groups, rowHref, canApply, working, refused, onApply, made, onUndo }: {
  groups: ProblemGroup[]
  rowHref: (rowId: number) => string
  canApply: boolean                    // not for an archived client or a closed year
  working: string                      // the suggestion being applied, if any
  refused: Record<string, string>      // why an Apply didn't go through, by suggestion
  onApply: (suggestion: Decision) => void
  made: Decision[]                     // the changes applied from this list since the page opened: each can be undone
  onUndo: (made: Decision) => void
}) {
  const count = problemCount(groups)
  return (
    <div className="review-run">
      <h3>Problems found {count > 0 && <span className="review-count">{count}</span>}</h3>
      {count === 0 && made.length === 0 && <p className="muted">No problems found.</p>}
      {made.map(d => (
        <div key={d.id} className="question-row">
          <span className="small">Applied: {d.rule.replace(/^R\d+\s+/, '')}. {d.reason}</span>
          <button type="button" className="btn btn-small" disabled={!canApply || !!working} onClick={() => onUndo(d)}>
            {working === d.id ? 'Undoing…' : 'Undo'}
          </button>
          {refused[d.id] && <span className="field-error">{refused[d.id]}</span>}
        </div>
      ))}
      {groups.map(group => (
        <div key={group.family}>
          <p className="muted small"><strong>{group.title}</strong></p>
          <ul className="decisions">
            {group.problems.map(p => (
              <li key={`${p.rowId}:${p.code}`}>
                <div>
                  <strong>{p.what}</strong> <Link href={rowHref(p.rowId)} className="small">Show</Link>
                </div>
                <div>{p.message}</div>
                {p.suggestion && (
                  <div className="question-row">
                    <span className="small">{suggested(p.suggestion, p.rowId)}</span>
                    <button type="button" className="btn btn-small" disabled={!canApply || !!working}
                            onClick={() => onApply(p.suggestion!)}>
                      {working === p.suggestion.id ? 'Applying…' : 'Apply'}
                    </button>
                    {refused[p.suggestion.id] && <span className="field-error">{refused[p.suggestion.id]}</span>}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}
