'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import './JobPanel.css'
import {
  cancelJob, checkJob, exportUrl, getEvents, getExceptions, getRequests, getSnapshots, lastSeq, latestJob, mergeEvents,
  pollDelay, requestStatusText, retryJob, STAGES, stageLabel, startJob, stepState,
  type AgentJob, type DocRequest, type JobEvent, type JobException, type JobStage, type Snapshot, type StepState,
} from '@/lib/agentJobs'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// Each step of the tracker: a mark, and always the same thing in words (colour only backs it up).
const STEP_MARK: Record<StepState, string> = { complete: '✓', working: '', stopped: '✖', waiting: '○', skipped: '–' }
const STEP_WORDS: Record<StepState, string> = {
  complete: 'complete', working: 'working', stopped: 'stopped here', waiting: 'waiting', skipped: 'not needed',
}
const STEP_CLASS: Record<StepState, string> = {
  complete: 'done', working: 'reading', stopped: 'failed', waiting: 'waiting', skipped: 'skipped',
}

const REQUEST_BADGE: Record<DocRequest['status'], string> = {
  pending: 'badge badge-warn', sent: 'badge badge-warn', receiving: 'badge badge-accent',
  received: 'badge badge-accent', replaced: 'badge badge-muted',
}

const clock = (at: string) => new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
const day = (at: string) => new Date(at).toLocaleDateString([], { day: 'numeric', month: 'short' })

// The client's agent job: start it for a period, watch its activity feed while it works, and, once it has ended, what
// it left for the accountant: the trial balance's workbook, the exceptions and the documents asked of the client.
// onRunningChange is told whenever the job starts or stops running, so the page can lock the rows while it works.
export default function JobPanel({ clientId, hasUploads, onRunningChange }: {
  clientId: number
  hasUploads: boolean
  onRunningChange: (running: boolean) => void
}) {
  const [job, setJob] = useState<AgentJob | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [events, setEvents] = useState<JobEvent[]>([])
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [exceptions, setExceptions] = useState<JobException[]>([])
  const [requests, setRequests] = useState<DocRequest[]>([])
  const [periodStart, setPeriodStart] = useState('')
  const [periodEnd, setPeriodEnd] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')            // a refused Start job, Cancel job or Retry, or a failed load
  const [watchError, setWatchError] = useState('')  // the last look at the running job that failed; cleared by the next that works
  const eventsRef = useRef<JobEvent[]>([])          // the feed as shown, read by the poll for the last seq it has
  const shownJob = useRef<string | null>(null)      // the job the feed is of
  const feed = useRef<HTMLOListElement>(null)
  const reported = useRef(false)
  const tell = useRef(onRunningChange)
  useEffect(() => { tell.current = onRunningChange })

  const showEvents = useCallback((next: JobEvent[]) => {
    eventsRef.current = next
    setEvents(next)
  }, [])

  // Shows a job. One that isn't the job shown before starts with a clean feed, and a job that is (about to be) running
  // has no results yet: they are read again when it ends.
  const adopt = useCallback((next: AgentJob) => {
    if (next.id !== shownJob.current) {
      shownJob.current = next.id
      showEvents([])
    }
    if (next.status === 'preparing') {
      setSnapshots([])
      setExceptions([])
      setRequests([])
    }
    setJob(next)
  }, [showEvents])

  // When the page opens: the client's latest job, if it has had one.
  useEffect(() => {
    let stale = false
    latestJob(clientId)
      .then(found => {
        if (stale) return
        if (found) {
          adopt(found)
          setPeriodStart(found.period_start)
          setPeriodEnd(found.period_end)
        }
        setLoaded(true)
      })
      .catch(e => { if (!stale) { setError(message(e)); setLoaded(true) } })
    return () => { stale = true }
  }, [clientId, adopt])

  // The client's latest job, when it is a different one from the one shown (the run that replaced it) or is running
  // (started elsewhere, say). Quiet when it can't be read: whatever called this has its own message.
  const showNewer = useCallback(() => {
    latestJob(clientId)
      .then(found => { if (found && (found.id !== shownJob.current || found.status === 'preparing')) adopt(found) })
      .catch(() => undefined)
  }, [clientId, adopt])

  const jobId = job?.id
  const watching = job !== null && pollDelay(job.status) !== null
  const running = job?.status === 'preparing'

  // While the job runs: look at it now and every 2 seconds, asking only for feed lines after the last one shown. A
  // look that fails (the API restarting, say) is shown and tried again; leaving the page stops it.
  useEffect(() => {
    if (!jobId || !watching) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const look = async () => {
      let next = pollDelay('preparing')
      try {
        const seen = await checkJob(jobId, lastSeq(eventsRef.current))
        if (stopped) return
        showEvents(mergeEvents(eventsRef.current, seen.events))
        setWatchError('')
        setJob(seen.job)
        next = pollDelay(seen.job.status)
        if (seen.job.status === 'superseded') showNewer()   // the client sent documents: a new run has started
      } catch (e) {
        if (stopped) return
        setWatchError(message(e))
      }
      if (next !== null) timer = setTimeout(look, next)
    }
    look()
    return () => { stopped = true; clearTimeout(timer) }
  }, [jobId, watching, showEvents, showNewer])

  // Once the job has left preparing (or was already over when the page opened): the rest of its feed, its trial
  // balance versions, its exceptions and its document requests, each read once.
  const ended = job !== null && !watching
  const endedStatus = job?.status
  useEffect(() => {
    if (!jobId || !ended) return
    let stale = false
    Promise.all([getEvents(jobId, lastSeq(eventsRef.current)), getSnapshots(jobId), getExceptions(jobId), getRequests(jobId)])
      .then(([more, versions, unsettled, asked]) => {
        if (stale) return
        showEvents(mergeEvents(eventsRef.current, more))
        setSnapshots(versions)
        setExceptions(unsettled)
        setRequests(asked)
      })
      .catch(e => { if (!stale) setError(message(e)) })
    return () => { stale = true }
  }, [jobId, ended, endedStatus, showEvents])

  useEffect(() => {
    const list = feed.current
    if (list) list.scrollTop = list.scrollHeight
  }, [events])

  // The page is told when running starts or stops, and that it stopped if the panel goes away mid-job.
  useEffect(() => {
    if (reported.current === running) return
    reported.current = running
    tell.current(running)
  }, [running])
  useEffect(() => () => {
    if (reported.current) {
      reported.current = false
      tell.current(false)
    }
  }, [])

  // Start job, Cancel job and Retry: what the API refuses (a job already running, no documents, a model not set) is
  // shown as it says it. The latest job is read again, in case the refusal is because a job is running that this
  // page didn't know of.
  const act = async (action: () => Promise<AgentJob>) => {
    setBusy(true)
    setError('')
    try {
      adopt(await action())
    } catch (e) {
      setError(message(e))
      showNewer()
    } finally {
      setBusy(false)
    }
  }
  const start = () => act(() => startJob(clientId, periodStart, periodEnd))
  const retry = () => job && act(() => retryJob(job.id))
  const cancel = () => job && act(() => cancelJob(job.id))

  const newest = snapshots.length ? snapshots[snapshots.length - 1] : null
  const canStart = !running && !busy && hasUploads && periodStart !== '' && periodEnd !== ''

  return (
    <section className="card section" aria-labelledby="job-heading">
      <div className="section-head">
        <h2 id="job-heading">Agent job</h2>
        {job && <span className="muted small">{job.period_start} to {job.period_end}</span>}
      </div>

      <div role="status">
        {!loaded ? <p className="muted">Loading…</p> : !job ? (
          <p className="muted">No job yet. Add this client&apos;s documents, choose the period and start a job.</p>
        ) : job.status === 'preparing' ? (
          <>
            <p>The agent is working. You can watch, but rows can&apos;t be edited until it finishes.</p>
            {job.cancel_requested && <p className="muted small">Cancelling: the job stops before its next step.</p>}
          </>
        ) : job.status === 'awaiting_review' ? (
          <div className="job-ready">
            <p className="text-ok"><span aria-hidden="true">✓ </span>Trial balance ready for review.</p>
            {newest && (
              <a className="btn" href={exportUrl(job.id, newest.version)} download>Download trial balance (Excel)</a>
            )}
          </div>
        ) : job.status === 'failed' ? (
          <div className="notice notice-error">
            <p>The job stopped: {job.error ?? 'no reason was given'}</p>
            <div className="form-actions">
              <button type="button" className="btn" onClick={retry} disabled={busy}>Retry from where it stopped</button>
            </div>
          </div>
        ) : job.status === 'cancelled' ? (
          <p className="muted">The job was cancelled. Start a new one when you&apos;re ready.</p>
        ) : (
          <p className="muted">A newer run replaced this one after the client sent documents.</p>
        )}
      </div>

      <div className="field-row job-dates">
        <label className="field">
          <span>Period start</span>
          <input className="input" type="date" value={periodStart} disabled={running}
                 onChange={e => setPeriodStart(e.target.value)} />
        </label>
        <label className="field">
          <span>Period end</span>
          <input className="input" type="date" value={periodEnd} disabled={running}
                 onChange={e => setPeriodEnd(e.target.value)} />
        </label>
      </div>
      <div className="form-actions">
        {running && (
          <button type="button" className="btn" onClick={cancel} disabled={busy || job.cancel_requested}>Cancel job</button>
        )}
        <button type="button" className="btn btn-primary" onClick={start} disabled={!canStart}
                title={hasUploads ? undefined : 'Add this client’s documents first'}>
          Start job
        </button>
      </div>

      {error && <div className="notice notice-error">{error}</div>}
      {watchError && <div className="notice notice-warn">{watchError} Trying again…</div>}

      {job && (
        <>
          <h3 className="job-subhead">Progress</h3>
          <ul className="batch" aria-label="Job stages">
            {STAGES.map((step: JobStage) => {
              const state = stepState(job, step)
              return (
                <li key={step} className={`batch-${STEP_CLASS[state]}`} aria-current={state === 'working' ? 'step' : undefined}>
                  <span aria-hidden="true">{state === 'working' ? <span className="spinner" /> : STEP_MARK[state]}</span>
                  <span className="batch-name">{stageLabel(step)}</span>
                  <span className="batch-detail">{STEP_WORDS[state]}</span>
                </li>
              )
            })}
          </ul>
          <h3 className="job-subhead">Activity</h3>
          {!events.length && <p className="muted small">{running ? 'Waiting for the agent’s first update…' : 'No activity was recorded.'}</p>}
          <ol ref={feed} className="job-feed" aria-label="Activity feed" aria-live="polite">
            {events.map(e => (
              <li key={e.seq}>
                <span className="mono muted">{clock(e.at)}</span>
                <span>{e.message}</span>
                <span className="muted small">{stageLabel(e.stage as JobStage)}</span>
              </li>
            ))}
          </ol>
        </>
      )}

      {job && !running && (
        <>
          <h3 className="job-subhead">Exceptions</h3>
          {!exceptions.length ? <p className="muted small">No exceptions.</p> : (
            <div className="table-card">
              <table className="table">
                <thead>
                  <tr><th>What couldn&apos;t be settled</th><th>What the agent assumed</th><th>Confidence</th><th>Status</th></tr>
                </thead>
                <tbody>
                  {exceptions.map(x => (
                    <tr key={x.id}>
                      <td>{x.description}</td>
                      <td>{x.assumption}</td>
                      <td className="job-confidence">{Math.round(x.confidence * 100)}%</td>
                      <td>
                        <span className={x.status === 'open' ? 'badge badge-warn' : 'badge badge-accent'}>
                          {x.status === 'open' ? 'Open' : 'Resolved'}
                        </span>
                        {x.status === 'resolved' && x.note && <div className="muted small">{x.note}</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <h3 className="job-subhead">Documents asked of the client</h3>
          {!requests.length ? <p className="muted small">No documents were asked for.</p> : (
            <div className="table-card">
              <table className="table">
                <thead><tr><th>Document</th><th>Period</th><th>Status</th></tr></thead>
                <tbody>
                  {requests.map(r => (
                    <tr key={r.id}>
                      <td>
                        {r.document_type}
                        <div className="muted small">{r.reason}</div>
                      </td>
                      <td>{r.period}</td>
                      <td>
                        <span className={REQUEST_BADGE[r.status]}>{requestStatusText(r.status)}</span>
                        {r.status === 'sent' && r.sent_at && <div className="muted small">Emailed {day(r.sent_at)}</div>}
                        {(r.status === 'received' || r.status === 'receiving') && r.received_at && (
                          <div className="muted small">Received {day(r.received_at)}</div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  )
}
