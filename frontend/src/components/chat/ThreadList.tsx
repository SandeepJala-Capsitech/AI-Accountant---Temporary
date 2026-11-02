'use client'

import Link from 'next/link'
import { groupThreads, type ChatThread } from '@/lib/chat'
import type { ClientSummary } from '@/lib/clients'

// The chats, grouped Today, Yesterday and Earlier, each with its title and client.
export default function ThreadList({ threads, clients, activeId, onOpen }: {
  threads: ChatThread[] | null
  clients: ClientSummary[] | null
  activeId: string | undefined
  onOpen: () => void           // a thread was clicked: on a phone, the list closes
}) {
  if (threads === null) return <p className="muted small thread-empty">Loading chats…</p>
  if (!threads.length) return <p className="muted small thread-empty">Start a chat to review a client&apos;s books.</p>
  const clientName = (id: number) => clients?.find(c => c.id === id)?.name ?? ''
  return (
    <nav aria-label="Chats">
      {groupThreads(threads, new Date()).map(group => (
        <div key={group.label} className="thread-group">
          <h3 className="thread-group-label">{group.label}</h3>
          <ul className="thread-list">
            {group.threads.map(t => (
              <li key={t.id}>
                <Link href={`/chat/${t.id}`} onClick={onOpen} aria-current={t.id === activeId ? 'page' : undefined}
                      className="thread-link">
                  <span className="thread-title">{t.title}</span>
                  <span className="thread-client">{clientName(t.client_id)}</span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  )
}
