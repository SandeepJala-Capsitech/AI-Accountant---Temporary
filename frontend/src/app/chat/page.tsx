'use client'

import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useState } from 'react'
import { useChatShell } from '@/components/chat/ChatContext'
import ClientPicker from '@/components/chat/ClientPicker'
import { newThread } from '@/lib/chat'
import type { ClientSummary } from '@/lib/clients'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// New chat: pick the client first. The thread is made on the client's open year, without calling the model.
export default function NewChatPage() {
  const router = useRouter()
  const { clients, reloadThreads } = useChatShell()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const pick = async (client: ClientSummary) => {
    setBusy(true)
    setError('')
    try {
      const thread = await newThread(client.id)
      await reloadThreads()
      router.push(`/chat/${thread.id}`)
    } catch (e) {
      setError(message(e))
      setBusy(false)
    }
  }

  if (clients === null) return <p className="muted">Loading clients…</p>
  if (!clients.length) {
    return (
      <div className="empty card">
        <h2>Start a chat to review a client&apos;s books</h2>
        <p className="muted">There are no clients yet.</p>
        <Link href="/" className="btn btn-primary">Add a client</Link>
      </div>
    )
  }
  return (
    <>
      {error && <div className="notice notice-error">{error}</div>}
      <ClientPicker clients={clients} busy={busy} onPick={pick} />
    </>
  )
}
