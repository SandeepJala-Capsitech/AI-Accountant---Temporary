'use client'

import Link from 'next/link'
import { useParams } from 'next/navigation'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useChatShell } from '@/components/chat/ChatContext'
import Composer from '@/components/chat/Composer'
import QuestionCard from '@/components/chat/QuestionCard'
import ReviewSummary from '@/components/chat/ReviewSummary'
import RunSteps from '@/components/chat/RunSteps'
import { ApiError } from '@/lib/api'
import {
  clientQuestions, fileStatus, getThread, retryFiles, stopRun, streamMessage,
  type ChatError, type ChatEvent, type ChatMessage, type Question, type ReviewSummary as Summary, type Step, type ThreadDetail,
} from '@/lib/chat'
import { getYears } from '@/lib/clients'
import { sectionHref } from '@/lib/years'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

const SUGGESTIONS = ["Review the documents I'm adding", 'What needs my attention?', 'Show the trial balance']

// One message sent from this page and what came back for it.
interface Turn {
  id: number
  text: string
  files: File[]
  bankAccounts: string[]
  steps: Step[]
  status: string
  reply: string
  error: ChatError | null
  running: boolean
}

// A chat about one client's year: the messages so far, the turn running now with its steps and streamed text, the
// summary the last turn ended with, and the message box. Answering a question replaces the summary in place.
export default function ThreadPage() {
  const { threadId } = useParams<{ threadId: string }>()
  const { clients, reloadThreads } = useChatShell()
  const [detail, setDetail] = useState<ThreadDetail | null>(null)
  const [history, setHistory] = useState<ChatMessage[]>([])
  const [turns, setTurns] = useState<Turn[]>([])
  const [summary, setSummary] = useState<Summary | null>(null)
  const [earlier, setEarlier] = useState<Question[]>([])
  const [yearLabel, setYearLabel] = useState('')
  const [draft, setDraft] = useState('')
  const [missing, setMissing] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const running = turns.some(t => t.running)
  const abort = useRef<AbortController | null>(null)
  const nextTurn = useRef(1)
  const end = useRef<HTMLDivElement>(null)

  const load = useCallback(async () => {
    try {
      const next = await getThread(threadId)
      setDetail(next)
      setHistory(next.messages)
      setTurns([])
      setSummary(next.summary)
      setMissing(false)
      setError('')
      const clientId = next.thread.client_id
      getYears(clientId).then(years => setYearLabel(years.find(y => y.start === next.thread.year_start)?.label ?? ''),
                              () => undefined)
      // A chat with nothing in it yet opens with what the client still owes us from earlier chats.
      if (!next.messages.length && !next.summary) clientQuestions(clientId).then(setEarlier, () => setEarlier([]))
    } catch (e) {
      if (e instanceof ApiError && e.code === 'http_404') setMissing(true)
      else setError(message(e))
    }
  }, [threadId])

  useEffect(() => {
    load()
    return () => abort.current?.abort()
  }, [load])

  useEffect(() => { end.current?.scrollIntoView?.({ block: 'end' }) }, [turns.length])

  const change = (id: number, patch: (t: Turn) => Partial<Turn>) =>
    setTurns(prev => prev.map(t => (t.id === id ? { ...t, ...patch(t) } : t)))

  const send = async (text: string, files: File[], bankAccounts: string[]) => {
    if (running) return
    const id = nextTurn.current++
    setTurns(prev => [...prev, { id, text, files, bankAccounts, steps: [], status: 'Working on it…', reply: '', error: null, running: true }])
    setNotice('')
    const controller = new AbortController()
    abort.current = controller
    let finished = false
    const onEvent = (event: ChatEvent) => {
      switch (event.type) {
        case 'status': change(id, () => ({ status: event.data.text })); break
        case 'step': change(id, t => ({
          steps: t.steps.some(s => s.id === event.data.id)
            ? t.steps.map(s => (s.id === event.data.id ? event.data : s))
            : [...t.steps, event.data],
        })); break
        case 'token': change(id, t => ({ reply: t.reply + event.data.text })); break
        case 'summary': setSummary(event.data); setEarlier([]); break
        case 'error': finished = true; change(id, () => ({ error: event.data })); break
        case 'done': finished = true; break
      }
    }
    try {
      await streamMessage(threadId, text, files, bankAccounts, onEvent, controller.signal)
      change(id, () => ({ running: false }))
      if (!finished) await lostConnection()
    } catch (e) {
      if (controller.signal.aborted) return
      if (e instanceof ApiError && e.code.startsWith('http_')) {
        change(id, () => ({ running: false, error: { code: e.code, message: e.message } }))
      } else {
        await lostConnection()
      }
    } finally {
      reloadThreads()
    }
  }

  // The stream broke off before the turn ended: what the server has is the truth, so the thread is loaded again.
  const lostConnection = async () => {
    await load()
    setNotice('The connection was lost, so the chat was loaded again.')
  }

  const stop = () => {
    const live = turns.find(t => t.running)
    if (live) change(live.id, () => ({ status: 'Stopping…' }))
    stopRun(threadId).catch((e: unknown) => setNotice(message(e)))
  }

  const retry = (turn: Turn) => {
    const again = retryFiles(turn.files, turn.bankAccounts, turn.steps)
    send(turn.text, again.files, again.bankAccounts)
  }

  if (missing) {
    return (
      <div className="empty card">
        <h2>Chat not found</h2>
        <p className="muted">It may have been deleted, or the link is wrong.</p>
        <Link href="/chat" className="btn">New chat</Link>
      </div>
    )
  }
  if (error) return <div className="notice notice-error">{error}</div>
  if (!detail) return <p className="muted">Loading…</p>

  const thread = detail.thread
  const client = clients?.find(c => c.id === thread.client_id)
  const fresh = !history.length && !turns.length
  const last = turns[turns.length - 1]
  return (
    <div className="chat-thread">
      <div className="chat-head">
        <h1>
          {client
            ? <Link href={sectionHref(client.id, thread.year_start, 'overview')} className="strong">{client.name}</Link>
            : 'Loading…'}
        </h1>
        <p className="muted">{[yearLabel, thread.title].filter(Boolean).join(' · ')}</p>
      </div>

      <div className="transcript">
        {history.map((m, i) => (
          <div key={i} className={m.role === 'user' ? 'bubble-user' : 'bubble-agent'}>{m.text}</div>
        ))}
        {turns.map(t => (
          <div key={t.id} className="turn">
            <div className="bubble-user">
              {t.text}
              {t.files.length > 0 && (
                <ul className="file-chips sent">
                  {t.files.map((f, i) => {
                    const status = fileStatus(f.name, t.steps)
                    return (
                      <li key={i} className={`file-chip file-${status.state}`} title={status.label}>
                        <span className="file-chip-name">{f.name}</span>
                        <span className="file-chip-status">{status.state === 'reading' && <span className="spinner" />} {status.label}</span>
                      </li>
                    )
                  })}
                </ul>
              )}
            </div>
            <div className="bubble-agent">
              <RunSteps steps={t.steps} status={t.status} running={t.running} />
              {t.reply && <p className="narrative">{t.reply}</p>}
              {t.error && t === last && <RunError error={t.error} onRetry={() => retry(t)} onContinue={() => send('Continue', [], [])} />}
            </div>
          </div>
        ))}
        {notice && <div className="notice notice-info">{notice}</div>}

        {!running && summary && (
          <ReviewSummary summary={summary} threadId={threadId} onSummary={setSummary} onSay={text => send(text, [], [])} />
        )}
        {!running && !summary && earlier.length > 0 && (
          <section className="review-needs" aria-label="Needs you">
            <h3>{earlier.length} {earlier.length === 1 ? 'thing still needs' : 'things still need'} you from earlier</h3>
            <ul className="questions">
              {earlier.map((q, i) => (
                <QuestionCard key={q.id} question={q} number={i + 1} threadId={threadId} onSummary={setSummary}
                              onSay={text => send(text, [], [])} />
              ))}
            </ul>
          </section>
        )}
        <div ref={end} />
      </div>

      {fresh && (
        <div className="suggestions">
          {SUGGESTIONS.map(s => (
            <button key={s} type="button" className="chip" onClick={() => setDraft(s)}>{s}</button>
          ))}
        </div>
      )}
      <Composer text={draft} onText={setDraft} bankAccounts={client?.bank_accounts ?? []} running={running}
                onSend={send} onStop={stop} />
    </div>
  )
}

// Why a turn ended early, with the way on: Retry (the model was unavailable or something failed), New chat, or
// Continue (the turn ran out of steps).
function RunError({ error, onRetry, onContinue }: { error: ChatError; onRetry: () => void; onContinue: () => void }) {
  if (error.code === 'loop_limit') {
    return (
      <div className="notice notice-warn">
        <span>Stopped after {error.steps ?? 'too many'} steps.</span>
        <button type="button" className="btn btn-small" onClick={onContinue}>Continue</button>
      </div>
    )
  }
  return (
    <div className="notice notice-error run-error">
      <span>{error.code === 'model_unavailable' ? `The model isn't available: ${error.message}` : error.message}</span>
      <span className="run-error-actions">
        <button type="button" className="btn btn-small" onClick={onRetry}>Retry</button>
        <Link href="/chat" className="btn btn-small btn-ghost">New chat</Link>
      </span>
    </div>
  )
}
