interface Props {
  label: string;
  tone?: "neutral" | "blue" | "gold" | "orange";
}

export function StatusBadge({ label, tone = "neutral" }: Props) {
  return <span className={`status-badge status-${tone}`}>{label}</span>;
}

