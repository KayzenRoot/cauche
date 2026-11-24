import { useEffect, useRef } from 'react'
import { ShieldAlert } from 'lucide-react'

export interface Confirmation {
  title: string
  description: string
  confirm: () => void
  destructive?: boolean
}

export function ConfirmDialog({ value, onClose }: {value: Confirmation; onClose: () => void}) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    ref.current?.showModal()
  }, [])
  return <dialog ref={ref} className="confirm-dialog" aria-labelledby="confirm-title" aria-describedby="confirm-description"
    onCancel={event => { event.preventDefault(); onClose() }}>
    <span className="empty-icon"><ShieldAlert size={22} aria-hidden="true" /></span>
    <h2 id="confirm-title">{value.title}</h2>
    <p id="confirm-description">{value.description}</p>
    <div className="dialog-actions">
      <button type="button" autoFocus onClick={onClose}>Voltar</button>
      <button type="button" className={value.destructive ? 'button-danger' : 'button-primary'} onClick={() => {
        value.confirm()
        onClose()
      }}>Confirmar</button>
    </div>
  </dialog>
}
