import { AlertTriangle, X } from "lucide-react";
import type { PreparedAction } from "../api/types";

interface Props {
  open: boolean;
  title: string;
  description: string;
  actionLabel: string;
  prepared: PreparedAction | null;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export function ConfirmDialog({ open, title, description, actionLabel, prepared, busy, onCancel, onConfirm }: Props) {
  if (!open) return null;
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onCancel}>
      <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title" onMouseDown={(e) => e.stopPropagation()}>
        <button className="icon-button dialog-close" onClick={onCancel} aria-label="关闭"><X size={18} /></button>
        <div className="dialog-alert"><AlertTriangle size={22} /></div>
        <h2 id="dialog-title">{title}</h2>
        <p>{description}</p>
        {prepared && (
          <dl className="confirmation-summary">
            {Object.entries(prepared.summary).map(([key, value]) => (
              <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{String(value ?? "—")}</dd></div>
            ))}
          </dl>
        )}
        {prepared?.blocker && <div className="inline-alert">无法确认：{prepared.blocker}</div>}
        <div className="dialog-actions">
          <button className="button button-secondary" onClick={onCancel}>取消</button>
          <button className="button button-danger" disabled={busy || !prepared?.can_confirm} onClick={onConfirm}>
            {busy ? "处理中…" : actionLabel}
          </button>
        </div>
      </section>
    </div>
  );
}

