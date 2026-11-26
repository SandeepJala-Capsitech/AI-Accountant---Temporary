// Signing in with the one username and password the API is given. The API keeps the session in a cookie the pages
// can't read; they only ask whether there is one, and go to /login when the API says there isn't.
import { request } from './api.ts'

export type Session = { auth_required: boolean; signed_in: boolean }

const JSON_WRITE = { 'X-LedgerSync': '1', 'Content-Type': 'application/json' }

export const getSession = () => request<Session>('/api/auth/session', {}, 10_000)

export const signIn = (username: string, password: string) =>
  request<void>('/api/auth/login', { method: 'POST', headers: JSON_WRITE, body: JSON.stringify({ username, password }) })

export const signOut = () => request<void>('/api/auth/logout', { method: 'POST', headers: { 'X-LedgerSync': '1' } })

export const loginPath = (current: string) => `/login?next=${encodeURIComponent(current)}`

// Where to go after signing in: a page of this site only, never another site a link put in ?next=.
export function safeNext(next: string | null): string {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/\\')) return '/'
  return next
}

// The API said this browser isn't signed in: off to /login, to come back here afterwards. Nothing in node (tests).
export function signedOut(): void {
  if (typeof window === 'undefined' || window.location.pathname === '/login') return
  window.location.assign(loginPath(window.location.pathname + window.location.search))
}
