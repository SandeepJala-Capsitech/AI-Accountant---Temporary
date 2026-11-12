'use client'

import Link from 'next/link'
import type { Decision } from '@/lib/chat'
import type { ProblemGroup } from '@/lib/problems'
import { problemCount } from '@/lib/problems'

// Every open problem of the year in one place, by kind: what the row is, what the check says, a link to the row,
// and, where a change is waiting for the person, what it would do and Apply.
export default function ProblemsList({ groups, rowHref, canApply, working, refused, onApply }: {
  groups: ProblemGroup[]
  rowHref: (rowId: number) => string
  canApply: boolean                    // not for an archived client or a closed year
  working: string                      // the suggestion being applied, if any
  refused: Record<string, string>      // why an Apply didn't go through, by suggestion
  onApply: (suggestion: Decision) => void
}) {
  const count = problemCount(groups)
  return (
    <div className="review-run">
      <h3>Problems found {count > 0 && <span className="review-count">{count}</span>}</h3>
      {count === 0 && <p className="muted">No problems found.</p>}
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
                    <span className="small">Suggested: change the account. {p.suggestion.reason}</span>
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
