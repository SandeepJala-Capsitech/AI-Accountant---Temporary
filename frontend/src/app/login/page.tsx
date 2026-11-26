'use client'

import { useState, type FormEvent } from 'react'
import { safeNext, signIn } from '@/lib/auth'

// Signing in with the one username and password the API was given; afterwards, back to the page that sent you here.
export default function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setSending(true)
    setError('')
    try {
      await signIn(username, password)
      window.location.assign(safeNext(new URLSearchParams(window.location.search).get('next')))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setSending(false)
    }
  }

  return (
    <form className="card dialog dialog-small login-form" onSubmit={submit}>
      <h2>Sign in</h2>
      {error && <p className="notice notice-error" role="alert">{error}</p>}
      <label className="field">
        Username
        <input className="input" autoFocus autoComplete="username" value={username}
               onChange={e => setUsername(e.target.value)} required />
      </label>
      <label className="field">
        Password
        <input className="input" type="password" autoComplete="current-password" value={password}
               onChange={e => setPassword(e.target.value)} required />
      </label>
      <div className="dialog-actions">
        <button type="submit" className="btn btn-primary" disabled={sending || !username || !password}>
          {sending ? 'Signing in…' : 'Sign in'}
        </button>
      </div>
    </form>
  )
}
