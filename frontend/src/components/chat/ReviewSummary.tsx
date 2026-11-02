'use client'

import type { ReviewSummary as Summary } from '@/lib/chat'
import DecisionLine from './DecisionLine'
import PostedTrialBalance from './PostedTrialBalance'
import QuestionCard from './QuestionCard'

// What a turn ends with, in this order: what was sorted, what needs the person, and the books as posted.
// The narrative above it is the turn's streamed text.
export default function ReviewSummary({ summary, threadId, onSummary, onSay }: {
  summary: Summary
  threadId: string
  onSummary: (summary: Summary) => void
  onSay: (text: string) => void
}) {
  const { sorted, questions, trial_balance: tb, post } = summary
  return (
    <div className="review">
      {sorted.length > 0 && (
        <section className="review-sorted" aria-label="Sorted">
          <h3>✓ Sorted</h3>
          <ul className="decisions">{sorted.map(d => <DecisionLine key={d.id} decision={d} />)}</ul>
        </section>
      )}
      {questions.length > 0 && (
        <section className="review-needs" aria-label="Needs you">
          <h3>Needs you <span className="review-count">{questions.length}</span></h3>
          <ul className="questions">
            {questions.map((q, i) => (
              <QuestionCard key={q.id} question={q} number={i + 1} threadId={threadId} onSummary={onSummary} onSay={onSay} />
            ))}
          </ul>
        </section>
      )}
      {post.problems.length > 0 && (
        <div className="notice notice-error">
          Nothing was posted: {post.problems.join(' ')}
        </div>
      )}
      <section aria-label="Posted"><PostedTrialBalance tb={tb} /></section>
    </div>
  )
}
