"use client";
export function HealthBadge({ value }: { value: unknown }) {
  const status = String(value || "Unknown");
  return (
    <span className={`risk-badge safety-${status.toLowerCase()}`}>
      {status}
    </span>
  );
}
