'use client'

import { useRouter } from 'next/navigation'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useClientYear } from '@/components/ClientYearContext'
import DocumentPanel, { type PanelDocument } from '@/components/DocumentPanel'
import ReviewHistory from '@/components/ReviewHistory'
import TodoList from '@/components/TodoList'
import { useRowDialogs } from '@/components/useRowDialogs'
import { clientDecisions, getMateriality } from '@/lib/api'
import { levelsLine, type Levels } from '@/lib/ledger'
import { clientQuestions, effectLine, type AnswerResult, type Decision, type Question } from '@/lib/chat'
import { changeRow, type RowChange } from '@/lib/clients'
import { DOCUMENT_TYPES } from '@/lib/clientRules'
import {
  activityLines, aiChanges, applySuggestion, clientAnswerer, lastReviewed, listReviews, listSuggestions, markReviewsSeen, newestFirst,
  retryReview, reviewNotice, undoDecision,
  type ReviewDetail,
} from '@/lib/reviews'
import { cardDocuments, sameFixGroups } from '@/lib/suggestions'
import { needCount, openQuestions, todoCards, waitingSuggestions } from '@/lib/todo'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))
const RELOAD_EVERY = 3_000     // while a review is running, or one was asked for
const GIVE_UP_AFTER = 120_000  // a review asked for with Try again that never starts

// What needs the person in a client's year, as one list: a card for each row or document with a problem, with the
// change the AI suggests for it and the question it asks about it. One notice says what stands in the way of a
// review (it failed, with Try again; it is paused; it is off). Below, closed until asked for: what the AI changed,
// each with Undo, and one line for each run. Opening the section marks the runs seen.
export default function AiReviewSection() {
  const { client, year, ledger, reload, show } = useClientYear()
  const router = useRouter()
  const [runs, setRuns] = useState<ReviewDetail[] | null>(null)
  const [ownQuestions, setOwnQuestions] = useState<Question[]>([])   // the client's questions, the books' own among them
  const [suggestions, setSuggestions] = useState<Decision[]>([])   // what waits for the person, the rules' too
  const [reversed, setReversed] = useState<Set<string>>(new Set()) // the changes some later decision undid
  const [freshRuns, setFreshRuns] = useState<Set<string> | null>(null)   // the runs not looked at when the page opened
  const [made, setMade] = useState<Decision[]>([])   // changes applied from the list on this visit
  const [error, setError] = useState('')
  const [working, setWorking] = useState('')
  const [problems, setProblems] = useState<Record<string, string>>({})   // a refused Undo or Apply, by decision
  const [undone, setUndone] = useState<Set<string>>(new Set())
  const [answers, setAnswers] = useState<{ id: string; text: string; effect: string }[]>([])   // answered on this visit
  // Try again was pressed: the newest run then, and the timer that stops waiting for a new one.
  const [asked, setAsked] = useState<{ newest: string | null } | null>(null)
  const [viewing, setViewing] = useState<string | null>(null)   // the card whose document is open beside the list
  const [levels, setLevels] = useState<Levels | null | undefined>(undefined)   // undefined: not loaded yet
  const { edit, add, dialogs } = useRowDialogs()
  const answerer = clientAnswerer(client.id)
  const readOnly = client.archived || year.closed

  const load = useCallback(async () => {
    try {
      const [reviews, waiting, decisions, questions] = await Promise.all([
        listReviews(client.id, year.start), listSuggestions(client.id, year.start), clientDecisions(client.id, year.start),
        clientQuestions(client.id),
      ])
      setOwnQuestions(questions)
      getMateriality(client.id, year.start).then(setLevels, () => setLevels(undefined))
      setRuns(newestFirst(reviews))
      setSuggestions(waiting)
      setReversed(new Set(decisions.flatMap(d => (d.reverses ? [d.reverses] : []))))
      setFreshRuns(fresh => fresh ?? new Set(reviews.filter(r => !r.seen).map(r => r.id)))
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

  // While a review runs, or one was asked for and hasn't started, the runs are read again every few seconds; when
  // it is over the year is read again too, with whatever the review changed, and its run is marked seen.
  const newest = runs?.[0]
  const awaited = !!asked && (newest?.id ?? null) === asked.newest
  const reviewing = awaited || newest?.status === 'running'
  const wasReviewing = useRef(false)
  useEffect(() => {
    if (reviewing) {
      wasReviewing.current = true
      const timer = setInterval(() => { load() }, RELOAD_EVERY)
      return () => clearInterval(timer)
    }
    if (wasReviewing.current) {
      wasReviewing.current = false
      reload()
      markReviewsSeen(client.id).catch(() => {})
    }
  }, [reviewing, load, reload, client.id])
  useEffect(() => {
    if (!asked) return
    const timer = setTimeout(() => setAsked(null), GIVE_UP_AFTER)
    return () => clearTimeout(timer)
  }, [asked])

  const retry = async () => {
    setError('')
    try {
      await retryReview(client.id)
      setAsked({ newest: newest?.id ?? null })
    } catch (e) {
      setError(message(e))
    }
  }

  // The cards: a problem with a whole document is named by its file, "Mark october expenses.xlsx, expense claim".
  const cards = useMemo(() => {
    const names = new Map(ledger.uploads.map(u => [u.id, u.name]))
    return todoCards(ledger.transactions, suggestions, openQuestions(runs ?? [], ownQuestions, year.start), row =>
      `${names.get(row.upload_id) ?? 'Document'}, ${(DOCUMENT_TYPES[row.document_type ?? 'receipt'] ?? 'document').toLowerCase()}`)
  }, [ledger.transactions, ledger.uploads, suggestions, runs, ownQuestions, year.start])
  const waiting = useMemo(() => waitingSuggestions(cards), [cards])
  const groups = useMemo(() => sameFixGroups(waiting), [waiting])
  // The documents open beside the list: every one its card is about, for as long as the card is on the list.
  const viewed = cards.find(c => c.key === viewing && c.row) ?? null
  const viewedDocuments: PanelDocument[] = viewed
    ? cardDocuments(viewed, ledger.transactions, ledger.uploads).map(({ upload, rowId }) => ({
        upload, rowId, rows: ledger.transactions.filter(r => r.upload_id === upload.id),
        quotes: viewed.suggestions.flatMap(d => d.evidence.filter(e => e.upload_id === upload.id).map(e => e.quote)),
      }))
    : []
  const panelOpen = viewedDocuments.length > 0
  const need = needCount(cards)
  const changes = useMemo(() => aiChanges(runs ?? [], new Set([...reversed, ...undone]), freshRuns ?? new Set()),
                          [runs, reversed, undone, freshRuns])
  const notice = awaited ? { kind: 'reviewing' as const, text: 'Reviewing…', retry: false } : reviewNotice(runs ?? [])
  const reviewed = lastReviewed(runs ?? [])

  const change = async (d: Decision, what: 'undo' | 'apply') => {
    setWorking(d.id)
    setProblems(p => ({ ...p, [d.id]: '' }))
    try {
      const done = await (what === 'undo' ? undoDecision : applySuggestion)(client.id, d.id)
      if (what === 'undo') {
        setUndone(u => new Set(u).add(d.id))
        setMade(m => m.filter(x => x.id !== d.id))
      }
      // The checks can refuse a suggestion the books have moved on from: nothing changed, and it says why.
      if (what === 'apply' && done.status === 'refused') setProblems(p => ({ ...p, [d.id]: `Not applied: ${done.reason}` }))
      // A change the person applied belongs to no review run, so it is kept here to be undone.
      if (what === 'apply' && done.status === 'applied') setMade(m => [...m, done])
      await load()
      await reload()
    } catch (e) {
      setProblems(p => ({ ...p, [d.id]: message(e) }))
    } finally {
      setWorking('')
    }
  }

  // These suggestions (all on the list, or those that make the same change), applied one after another; one the
  // checks refuse says why on its card and the rest go on.
  const applyThese = async (these: Decision[], key: string) => {
    setWorking(key)
    for (const d of these) {
      setProblems(p => ({ ...p, [d.id]: '' }))
      try {
        const done = await applySuggestion(client.id, d.id)
        if (done.status === 'refused') setProblems(p => ({ ...p, [d.id]: `Not applied: ${done.reason}` }))
        if (done.status === 'applied') setMade(m => [...m, done])
      } catch (e) {
        setProblems(p => ({ ...p, [d.id]: message(e) }))
      }
    }
    await load()
    await reload()
    setWorking('')
  }

  // A fix made on a card, as on the Transactions page: the year is shown as the API answers, and the suggestions
  // are read again, since a row that has changed can leave one behind.
  const changeARow = async (rowId: number, made: RowChange) => {
    setError('')
    try {
      show(await changeRow(client.id, rowId, made))
      await load()
    } catch (e) {
      setError(message(e))
    }
  }

  const answered = async (a: AnswerResult) => {
    // A question that is settled leaves the list, so what its answer did is said above it. One answered "I don't
    // have it" stays on the list, marked as waiting, and needs no line.
    if (a.question.status === 'resolved') {
      setAnswers(list => [...list.filter(x => x.id !== a.question.id),
                          { id: a.question.id, text: a.question.text, effect: effectLine(a) ?? 'Answered.' }])
    }
    await load()
    await reload()
  }

  const say = (text: string) => router.push(`/chat?client=${client.id}&ask=${encodeURIComponent(text)}`)

  return (
    <section className="card section">
      <div className="section-head"><h2>AI review</h2></div>
      {error && <div className="notice notice-error">{error}</div>}
      {notice && (
        <div className={notice.kind === 'failed' ? 'notice notice-warn' : 'notice notice-info'}>
          <span>{notice.kind === 'reviewing' && <span className="spinner" aria-hidden="true" />} {notice.text}</span>
          {notice.retry && (
            <button type="button" className="btn btn-small" disabled={readOnly} onClick={retry}>Try again</button>
          )}
        </div>
      )}
      {answers.map(a => <p key={a.id} className="muted small todo-done">{a.text} {a.effect}</p>)}
      {!runs && !error && <p className="muted">Loading the reviews…</p>}
      {runs && (
        <>
          <div className="todo-top">
            {cards.length > 0 ? (
              <h3>
                {need > 0 ? `${need} ${need === 1 ? 'thing needs' : 'things need'} you` : 'Nothing needs you now'}
                {cards.length > need && <span className="muted small"> · {cards.length - need} waiting on you</span>}
              </h3>
            ) : (
              <p className="muted">
                Nothing needs you.{' '}
                {reviewed ? `Last reviewed ${reviewed}.`
                  : runs.length === 0 ? 'No review has run on this year yet. One runs after the books change and go quiet.' : ''}
              </p>
            )}
            {waiting.length > 1 && (
              <button type="button" className="btn btn-small btn-primary" disabled={!!working || readOnly} onClick={() => applyThese(waiting, 'all')}>
                {working === 'all' ? 'Applying…' : `Apply all ${waiting.length} suggestions`}
              </button>
            )}
          </div>
          {/* The same change suggested for several rows, applied together and apart from the rest. */}
          {groups.map(group => (
            <div key={group.key} className="question-row todo-group">
              <span className="small">{group.label}</span>
              <button type="button" className="btn btn-small" disabled={!!working || readOnly}
                      onClick={() => applyThese(group.suggestions, group.key)}>
                {working === group.key ? 'Applying…' : `Apply to all ${group.suggestions.length}`}
              </button>
            </div>
          ))}
          {levels !== undefined && <p className="hint">{levelsLine(levels)}</p>}
          <div className={panelOpen ? 'todo-with-panel todo-panel-open' : 'todo-with-panel'}>
            <TodoList cards={cards} working={working} refused={problems} answerer={answerer} viewing={panelOpen ? viewing : null}
                      onView={card => setViewing(v => (v === card.key ? null : card.key))} onChange={changeARow}
                      onApply={d => change(d, 'apply')} onEdit={edit} onAddLine={add} onAnswered={answered} onSay={say} />
            {panelOpen && <DocumentPanel documents={viewedDocuments} onClose={() => setViewing(null)} />}
          </div>
          <ReviewHistory changes={changes} made={made} lines={activityLines(runs)} readOnly={readOnly} working={working}
                         refused={problems} onUndo={d => change(d, 'undo')} />
        </>
      )}
      {dialogs}
    </section>
  )
}
