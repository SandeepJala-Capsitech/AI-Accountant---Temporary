'use client'

import { useRouter } from 'next/navigation'
import { useCallback, useEffect, useMemo, useState } from 'react'
import DecisionLine from '@/components/chat/DecisionLine'
import ProblemsList from '@/components/ProblemsList'
import QuestionCard from '@/components/chat/QuestionCard'
import { useClientYear } from '@/components/ClientYearContext'
import { effectLine, type AnswerResult, type Decision } from '@/lib/chat'
import {
  applySuggestion, clientAnswerer, isReversed, listReviews, listSuggestions, markReviewsSeen, newestFirst, runSaid, runTitle, runWhen,
  undoDecision,
  type ReviewDetail,
} from '@/lib/reviews'
import { groupProblems } from '@/lib/problems'
import { sectionHref } from '@/lib/years'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// What the background AI review did after the client's books changed: each run, newest first, with the changes it
// made (Undo), the ones it only suggests (Apply) and the questions it asks. Opening the section marks them seen.
const refusedLine = (n: number) =>
  `${n} other change${n === 1 ? '' : 's'} the AI tried ${n === 1 ? 'was' : 'were'} stopped by the checks and not made.`

export default function AiReviewSection() {
  const { client, year, ledger, reload } = useClientYear()
  const router = useRouter()
  const [runs, setRuns] = useState<ReviewDetail[] | null>(null)
  const [suggestions, setSuggestions] = useState<Decision[]>([])   // what waits for the person, the rules' too
  const [error, setError] = useState('')
  const [open, setOpen] = useState<Set<string>>(new Set())
  const [working, setWorking] = useState('')
  const [problems, setProblems] = useState<Record<string, string>>({})   // a refused Undo or Apply, by decision
  const [undone, setUndone] = useState<Set<string>>(new Set())
  const [effects, setEffects] = useState<Record<string, string>>({})
  const answerer = clientAnswerer(client.id)

  const load = useCallback(async () => {
    try {
      const [reviews, waiting] = await Promise.all([listReviews(client.id, year.start), listSuggestions(client.id, year.start)])
      setRuns(newestFirst(reviews))
      setSuggestions(waiting)
      setError('')
      return true
    } catch (e) {
      setError(message(e))
      return false
    }
  }, [client.id, year.start])

  useEffect(() => {
    load().then(ok => { if (ok) markReviewsSeen(client.id).catch(() => {}) })
  }, [load, client.id])

  // The year's open problems, from the rows the page already has, each with the suggestion waiting for it.
  const groups = useMemo(() => groupProblems(ledger.transactions, suggestions), [ledger.transactions, suggestions])
  // A problem's row on the Transactions page, beside the rows the problem names.
  const rowHref = (rowId: number) => `${sectionHref(client.id, year.start, 'transactions')}?row=${rowId}`

  const toggle = (id: string) => setOpen(o => { const n = new Set(o); if (n.has(id)) n.delete(id); else n.add(id); return n })

  const change = async (d: Decision, what: 'undo' | 'apply') => {
    setWorking(d.id)
    setProblems(p => ({ ...p, [d.id]: '' }))
    try {
      const made = await (what === 'undo' ? undoDecision : applySuggestion)(client.id, d.id)
      if (what === 'undo') setUndone(u => new Set(u).add(d.id))
      // The checks can refuse a suggestion the books have moved on from: nothing changed, and it says why.
      if (what === 'apply' && made.status === 'refused') setProblems(p => ({ ...p, [d.id]: `Not applied: ${made.reason}` }))
      await load()
      await reload()
    } catch (e) {
      setProblems(p => ({ ...p, [d.id]: message(e) }))
    } finally {
      setWorking('')
    }
  }

  // Every suggestion of a run still waiting, applied one after another; one the checks refuse says why and the
  // rest go on.
  const applyAll = async (waiting: Decision[]) => {
    setWorking('all')
    for (const d of waiting) {
      setProblems(p => ({ ...p, [d.id]: '' }))
      try {
        await applySuggestion(client.id, d.id)
      } catch (e) {
        setProblems(p => ({ ...p, [d.id]: message(e) }))
      }
    }
    await load()
    await reload()
    setWorking('')
  }

  const answered = async (a: AnswerResult) => {
    const line = effectLine(a)
    if (line) setEffects(e => ({ ...e, [a.question.id]: line }))
    await load()
    await reload()
  }

  const say = (text: string) => router.push(`/chat?client=${client.id}&ask=${encodeURIComponent(text)}`)

  const body = (run: ReviewDetail) => (
    <>
      {run.status !== 'failed' && <p className="muted">{runSaid(run)}</p>}
      {/* A change the checks refused was never made, and its reason is written for the AI to try again: only how
          many there were is said. */}
      {run.decisions.some(d => d.status === 'refused') && (
        <p className="muted small">
          {refusedLine(run.decisions.filter(d => d.status === 'refused').length)}
        </p>
      )}
      {run.decisions.some(d => d.status !== 'refused') && (
        <div className="decisions">
          {(() => {
            const waiting = run.decisions.filter(d => d.status === 'suggested' && !run.applied[d.id])
            return waiting.length > 1 && (
              <div className="question-row">
                <button type="button" className="btn btn-small btn-primary"
                        disabled={!!working || client.archived || year.closed} onClick={() => applyAll(waiting)}>
                  {working === 'all' ? 'Applying…' : `Apply all ${waiting.length}`}
                </button>
                <span className="muted small">These are suggestions: nothing has changed yet.</span>
              </div>
            )
          })()}
          {run.decisions.filter(d => d.status !== 'refused').map(d => {
            const suggestion = d.status === 'suggested'
            const applied = suggestion && !!run.applied[d.id]
            const reversed = isReversed(run, d.id) || undone.has(d.id)
            return (
              <div key={d.id}>
                <ul className="decisions"><DecisionLine decision={d} /></ul>
                <div className="question-row">
                  {suggestion && !applied && (
                    <button type="button" className="btn btn-small" disabled={!!working || client.archived || year.closed}
                            onClick={() => change(d, 'apply')}>Apply</button>
                  )}
                  {suggestion && applied && <span className="muted small">Applied</span>}
                  {d.status === 'applied' && !d.reverses && !reversed && (
                    <button type="button" className="btn btn-small" disabled={working === d.id || client.archived || year.closed}
                            onClick={() => change(d, 'undo')}>Undo</button>
                  )}
                  {d.status === 'applied' && !d.reverses && reversed && <span className="muted small">Undone</span>}
                  {problems[d.id] && <span className="field-error">{problems[d.id]}</span>}
                </div>
              </div>
            )
          })}
        </div>
      )}
      {run.questions.length > 0 && (
        <ul className="questions">
          {run.questions.map((q, i) => (
            <QuestionCard key={q.id} question={q} number={i + 1} answerer={answerer} onAnswered={answered} onSay={say}
                          effect={effects[q.id] ?? null} busy={false} />
          ))}
        </ul>
      )}
    </>
  )

  return (
    <section className="card section">
      <div className="section-head"><h2>AI review</h2></div>
      {error && <div className="notice notice-error">{error}</div>}
      <ProblemsList groups={groups} rowHref={rowHref} canApply={!client.archived && !year.closed} working={working}
                    refused={problems} onApply={d => change(d, 'apply')} />
      {!runs && !error && <p className="muted">Loading the reviews…</p>}
      {runs && runs.length === 0 && <p className="muted">No review has run on this year yet. One runs after the books change and go quiet.</p>}
      {runs?.map(run => (
        <div key={run.id} className="review-run">
          {run.status === 'limited' ? (
            <p className="muted">{runWhen(run.started_at)}: AI review paused for today: limit reached</p>
          ) : run.status === 'off' ? (
            <p className="muted">{runWhen(run.started_at)}: AI review is off for this client</p>
          ) : (
            <>
              <button type="button" className="decision-head" aria-expanded={open.has(run.id)} onClick={() => toggle(run.id)}>
                <span className="decision-caret" aria-hidden="true" />
                <span className="decision-text">
                  <strong>{run.status === 'failed' ? [runWhen(run.started_at), run.trigger_summary].filter(Boolean).join(', ') : runTitle(run)}</strong>
                  {!run.seen && <> <span className="badge badge-accent">New</span></>}
                  {run.status === 'running' && <> <span className="progress"><span className="spinner" /> Reviewing…</span></>}
                </span>
              </button>
              {run.status === 'failed' && <p className="field-error">The review didn’t finish{run.error ? `: ${run.error}` : '.'}</p>}
              {open.has(run.id) && run.status !== 'running' && body(run)}
            </>
          )}
        </div>
      ))}
    </section>
  )
}
