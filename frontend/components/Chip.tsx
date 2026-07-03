"use client";

export function Chip({ label, onRemove }: { label: string; onRemove?: () => void }) {
  return (
    <span className="chip">
      {label}
      {onRemove && (
        <span className="x" onClick={onRemove} role="button" aria-label={`remove ${label}`}>
          ✕
        </span>
      )}
    </span>
  );
}

export function SuggestChip({ label, onAdd }: { label: string; onAdd: () => void }) {
  return (
    <span className="ghost suggest" onClick={onAdd} role="button">
      ✦ {label}
    </span>
  );
}
