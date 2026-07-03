"use client";

import { useEffect, useState } from "react";

const KEY = "omniboard.trainingBannerDismissed";

export function TrainingBanner() {
  const [hidden, setHidden] = useState(true);
  useEffect(() => setHidden(localStorage.getItem(KEY) === "1"), []);
  if (hidden) return null;
  return (
    <div className="training-banner">
      <div className="text">
        ⓘ Ticking and crossing roles trains your search — results improve over time.
      </div>
      <button
        className="dismiss"
        onClick={() => {
          localStorage.setItem(KEY, "1");
          setHidden(true);
        }}
      >
        dismiss
      </button>
    </div>
  );
}
