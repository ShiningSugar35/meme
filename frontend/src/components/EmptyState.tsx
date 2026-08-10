import { CircleDashed } from "lucide-react";

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty-state">
      <CircleDashed size={24} />
      <strong>{title}</strong>
      <span>{detail}</span>
    </div>
  );
}

