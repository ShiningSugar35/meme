import type { LucideIcon } from "lucide-react";

interface Props {
  title: string;
  value: string;
  context?: string;
  icon: LucideIcon;
  tone?: "blue" | "gold" | "orange" | "neutral";
}

export function MetricCard({ title, value, context, icon: Icon, tone = "neutral" }: Props) {
  return (
    <article className="metric-card">
      <div className={`metric-icon metric-${tone}`}><Icon size={18} strokeWidth={1.8} /></div>
      <div>
        <p className="eyebrow">{title}</p>
        <strong className="metric-value">{value}</strong>
        {context && <p className="metric-context">{context}</p>}
      </div>
    </article>
  );
}

