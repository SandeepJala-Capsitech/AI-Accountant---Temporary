'use client'

import { useCallback, useRef, useState } from 'react'

// A question asked before something hard to undo, in the page's own style: ask() resolves true on confirm.
export function useConfirm() {
  const [question, setQuestion] = useState<{ text: string; action: string } | null>(null)
  const answer = useRef<(yes: boolean) => void>(() => {})

  const ask = useCallback((text: string, action: string) => new Promise<boolean>(resolve => {
    answer.current = resolve
    setQuestion({ text, action })
  }), [])

  const reply = (yes: boolean) => {
    setQuestion(null)
    answer.current(yes)
  }

  const dialog = question && (
    <div className="overlay" role="presentation" onKeyDown={e => { if (e.key === 'Escape') reply(false) }}>
      <div className="dialog dialog-small" role="alertdialog" aria-modal="true" aria-describedby="confirm-text">
        <p id="confirm-text">{question.text}</p>
        <div className="dialog-actions">
          <button type="button" className="btn btn-ghost" onClick={() => reply(false)} autoFocus>Cancel</button>
          <button type="button" className="btn btn-danger" onClick={() => reply(true)}>{question.action}</button>
        </div>
      </div>
    </div>
  )
  return { ask, dialog }
}
