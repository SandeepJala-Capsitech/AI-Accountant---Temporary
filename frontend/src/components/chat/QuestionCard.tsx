'use client'

import { useRef, useState } from 'react'
import { answerQuestion, questionControl, questionState, uploadForQuestion, type Answer, type AnswerResult, type Question } from '@/lib/chat'
import type { Answerer } from '@/lib/reviews'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// One question, in amber: what is asked, why it matters, and its answer control. A date or a document goes to the
// question's own route, which returns that question with its effect; a question in words is answered as a chat message.
export default function QuestionCard({ question, number, threadId, answerer, onAnswered, onSay, effect, busy: working }: {
  question: Question
  number: number
  threadId?: string                  // the chat the card is in; the default answerer uses its routes
  answerer?: Answerer                // answers some other way, such as a background review's client routes
  onAnswered: (a: AnswerResult) => void
  onSay: (text: string) => void      // sends a chat message on this thread
  effect: string | null              // what the answer did, shown on every card for this question
  busy: boolean                      // a turn is running: the controls wait, keeping what was typed
}) {
  const [date, setDate] = useState('')
  const [words, setWords] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)
  const control = questionControl(question)
  const state = questionState(question)

  const run = async (what: string, call: () => Promise<AnswerResult>) => {
    setBusy(what)
    setError('')
    try {
      const result = await call()
      onAnswered(result)
    } catch (e) {
      setError(message(e))
    } finally {
      setBusy('')
    }
  }
  const how: Answerer = answerer ?? {
    answer: (qid, a) => answerQuestion(threadId ?? '', qid, a),
    upload: (qid, file) => uploadForQuestion(threadId ?? '', qid, file),
  }
  const answer = (a: Answer, what: string) => run(what, () => how.answer(question.id, a))
  const upload = (file: File | undefined) => {
    if (file) run(`Reading ${file.name}…`, () => how.upload(question.id, file))
  }
  const say = () => {
    if (!words.trim()) return
    onSay(`Answer to "${question.text}": ${words.trim()}`)
    setWords('')
  }

  return (
    <li className={question.status === 'waiting' ? 'question question-waiting' : 'question'}>
      <p className="question-text"><strong>{number}. {question.text}</strong></p>
      <p className="question-why">{question.why}</p>
      {state && <p className="question-state">{state}</p>}
      {question.status !== 'resolved' && <div className="question-controls">
        {control === 'date' && (
          <form className="question-row" onSubmit={e => { e.preventDefault(); if (date) answer({ kind: 'date', date }, 'Saving…') }}>
            <input className="input question-date" type="date" value={date} onChange={e => setDate(e.target.value)}
                   aria-label={question.text} disabled={!!busy || working} />
            <button type="submit" className="btn btn-primary btn-small" disabled={!date || !!busy || working}>Use this date</button>
            <button type="button" className="btn btn-small" disabled={!!busy || working}
                    onClick={() => answer({ kind: 'not_sure' }, 'Saving…')}>Not sure</button>
          </form>
        )}
        {control === 'upload' && (
          <div className="question-row">
            <input ref={fileInput} type="file" hidden onChange={e => { upload(e.target.files?.[0]); e.target.value = '' }} />
            <button type="button" className="btn btn-primary btn-small" disabled={!!busy || working}
                    onClick={() => fileInput.current?.click()}>📎 Upload</button>
            <button type="button" className="btn btn-small" disabled={!!busy || working}
                    onClick={() => answer({ kind: 'no_document' }, 'Saving…')}>I don&apos;t have it</button>
          </div>
        )}
        {control === 'text' && (
          <form className="question-row" onSubmit={e => { e.preventDefault(); say() }}>
            <input className="input" value={words} onChange={e => setWords(e.target.value)} aria-label={question.text}
                   placeholder="Your answer" disabled={working} />
            <button type="submit" className="btn btn-primary btn-small" disabled={!words.trim() || working}>Send</button>
          </form>
        )}
        {working && !busy && <span className="muted">You can answer when the current reply finishes.</span>}
        {busy && <span className="progress question-busy"><span className="spinner" /> {busy}</span>}
      </div>}
      {effect && <p className="question-effect">{effect}</p>}
      {error && <p className="field-error">{error}</p>}
    </li>
  )
}
