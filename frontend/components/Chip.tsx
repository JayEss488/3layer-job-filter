"use client";

export function Chip({
  label,
  onRemove,
  pinned,
  onTogglePin,
}: {
  label: string;
  onRemove?: () => void;
  /** Reuses the .chip.tinted style to visually mark a pinned (won't be
   * auto-replaced) chip -- e.g. a confirmed target role. */
  pinned?: boolean;
  onTogglePin?: () => void;
}) {
  return (
    <span className={pinned ? "chip tinted" : "chip"}>
      {onTogglePin && (
        <span
          className="pin-toggle"
          onClick={onTogglePin}
          role="button"
          title={
            pinned
              ? "Pinned — won't change when roles regenerate. Click to unpin."
              : "Not pinned — may be replaced next regeneration. Click to pin."
          }
        >
          {pinned ? "📌" : "📍"}
        </span>
      )}
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
