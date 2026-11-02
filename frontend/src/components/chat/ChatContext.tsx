'use client'

import { createContext, useContext } from 'react'
import type { ChatThread } from '@/lib/chat'
import type { ClientSummary } from '@/lib/clients'

// What the chat pages share from their layout: the clients, every thread, and a reload once a thread changes.
export interface ChatShell {
  clients: ClientSummary[] | null    // null until they arrive
  threads: ChatThread[] | null
  reloadThreads: () => Promise<void>
  error: string
}

export const ChatShellContext = createContext<ChatShell | null>(null)

export function useChatShell(): ChatShell {
  const shell = useContext(ChatShellContext)
  if (!shell) throw new Error('useChatShell is used outside the chat layout')
  return shell
}
