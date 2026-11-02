'use client'

import Link from 'next/link'
import { useParams } from 'next/navigation'
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { ChatShellContext, type ChatShell } from '@/components/chat/ChatContext'
import ThreadList from '@/components/chat/ThreadList'
import { listThreads, type ChatThread } from '@/lib/chat'
import { listClients, type ClientSummary } from '@/lib/clients'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// The chat: the thread list on the left (behind a button on a phone) and a thread, or New chat, beside it.
export default function ChatLayout({ children }: { children: ReactNode }) {
  const params = useParams<{ threadId?: string }>()
  const [clients, setClients] = useState<ClientSummary[] | null>(null)
  const [threads, setThreads] = useState<ChatThread[] | null>(null)
  const [error, setError] = useState('')
  const [listOpen, setListOpen] = useState(false)

  const reloadThreads = useCallback(async () => {
    try {
      setThreads(await listThreads())
    } catch (e) {
      setError(message(e))
    }
  }, [])

  useEffect(() => {
    listClients().then(setClients, (e: unknown) => setError(message(e)))
    reloadThreads()
  }, [reloadThreads])

  const shell = useMemo<ChatShell>(() => ({ clients, threads, reloadThreads, error }), [clients, threads, reloadThreads, error])
  return (
    <ChatShellContext.Provider value={shell}>
      <div className="chat-space">
        <aside className={listOpen ? 'chat-side open' : 'chat-side'} id="chat-threads">
          <Link href="/chat" className="btn btn-primary chat-new" onClick={() => setListOpen(false)}>New chat</Link>
          <ThreadList threads={threads} clients={clients} activeId={params.threadId} onOpen={() => setListOpen(false)} />
        </aside>
        <section className="chat-main">
          <button type="button" className="btn btn-ghost btn-small chat-side-toggle" aria-expanded={listOpen}
                  aria-controls="chat-threads" onClick={() => setListOpen(o => !o)}>
            {listOpen ? 'Hide chats' : 'Chats'}
          </button>
          {error && <div className="notice notice-error">{error}</div>}
          {children}
        </section>
      </div>
    </ChatShellContext.Provider>
  )
}
