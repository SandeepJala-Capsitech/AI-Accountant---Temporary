'use client'

import { createContext, useContext } from 'react'
import type { Client, Ledger, Year } from '@/lib/clients'

// What every section of a client's year is given by the layout around it.
export interface ClientYear {
  client: Client
  year: Year
  years: Year[]
  ledger: Ledger             // the year's rows and uploads
  reload: () => Promise<void>   // fetches the year again, after a change
  error: string              // why the last load failed; '' when it worked
}

export const ClientYearContext = createContext<ClientYear | null>(null)

export function useClientYear(): ClientYear {
  const value = useContext(ClientYearContext)
  if (!value) throw new Error('useClientYear is used inside a client year page.')
  return value
}
