"use client";

export function Chip({
  label,
  onRemove,
  variant,
}: {
  label: string;
  onRemove?: () => void;
  variant?: "exp";
}) {
  return (
    <span className={variant ? `chip ${variant}` : "chip"}>
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
