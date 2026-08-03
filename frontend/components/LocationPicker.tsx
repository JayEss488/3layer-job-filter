"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
// COUNTRY_CHOICES (full worldwide dropdown) and COUNTRY_TOKENS (name/alias + a
// few major cities, for auto-highlighting the inferred country) are generated
// alongside the engine's country tokens and the backend list by
// scripts/gen_countries.py, so all three stay in sync. Regenerate to refresh.
import { COUNTRY_CHOICES, COUNTRY_TOKENS } from "@/lib/countries.gen";
import { useAttributeMutations } from "@/lib/hooks";
import type { Attribute } from "@/lib/types";

// Work types share the `location` attribute type with the free-text place name
// (the backend splits them apart on exactly this set — see config.WORK_TYPE_VALUES
// and snapshot.build_snapshot). They render as their own preference row now
// (WorkStylePicker), because they're a different constraint with its own
// Hard/Soft: the place drives the country prefilter, the work type drives the
// screen's work-arrangement axis. This picker still has to know the set so it
// can tell a place row from a work-type row.
export const WORK_TYPES = ["On-site", "Hybrid", "Remote"];
export const WORK_SET = new Set([...WORK_TYPES.map((w) => w.toLowerCase()), "onsite"]);

// Ordered narrowest -> broadest: each scope is a superset of the ones before it
// (national search results already include local-place matches; international
// already includes national + local, since it just drops the country filter).
// The UI highlights every choice up to and including the selected one so that
// superset relationship is visible, not just the single active radio value.
const SCOPE_CHOICES: { value: string; label: string }[] = [
  { value: "local", label: "Local" },
  { value: "national", label: "National" },
  { value: "international", label: "International" },
];
const SCOPE_ORDER = SCOPE_CHOICES.map((s) => s.value);

function detectCountry(text: string): string | null {
  const loc = (text || "").trim().toLowerCase();
  if (!loc) return null;
  for (const [code, tokens] of Object.entries(COUNTRY_TOKENS)) {
    if (tokens.some((t) => loc.includes(t))) return code;
  }
  return null;
}

/** City/region free text, how far to search from it, and which countries count. */
export function LocationPicker({
  profileId,
  attributes,
  countryAttributes,
  scopeAttributes,
}: {
  profileId: number;
  attributes: Attribute[];
  countryAttributes: Attribute[];
  scopeAttributes: Attribute[];
}) {
  const { add, remove, invalidate } = useAttributeMutations(profileId);

  const cityAttr = attributes.find((a) => !WORK_SET.has(a.value.toLowerCase()));

  const selectedCountries = new Map(
    countryAttributes.map((a) => [a.value.toLowerCase(), a])
  );

  // Single-value, like seniority: at most one location_scope row.
  const scope = scopeAttributes[0]?.value.toLowerCase() || "national";
  function setScope(value: string) {
    if (scope === value) return;
    scopeAttributes.forEach((a) => remove.mutate(a.id));
    add.mutate({ type: "location_scope", value });
  }
  // Scope becomes the sole authority on whether a country filter applies at
  // all once it's "local" or "international" -- showing country chips there
  // would let them silently contradict the scope setting.
  const showCountryChips = scope === "national";

  function toggleCountry(code: string) {
    if (code === "global") {
      // Selecting Global clears every other selection.
      countryAttributes.forEach((a) => remove.mutate(a.id));
      if (!selectedCountries.has("global")) add.mutate({ type: "country", value: "global" });
      return;
    }
    const globalAttr = selectedCountries.get("global");
    if (globalAttr) remove.mutate(globalAttr.id); // picking a real country clears Global

    const existing = selectedCountries.get(code);
    if (existing) remove.mutate(existing.id);
    else add.mutate({ type: "country", value: code });
  }

  const [city, setCity] = useState(cityAttr?.value ?? "");
  useEffect(() => setCity(cityAttr?.value ?? ""), [cityAttr?.value]);

  // Country auto-detected from the typed city/region. When the user hasn't
  // explicitly picked a country, this is the one the backend will filter by
  // (fail-closed default), so highlight it to make that visible.
  const detected = detectCountry(city);
  const noneSelected = selectedCountries.size === 0;

  async function persistCity() {
    const v = city.trim();
    if (cityAttr && v && v !== cityAttr.value) {
      await api.updateAttribute(cityAttr.id, { value: v });
      invalidate();
    } else if (cityAttr && !v) {
      await api.deleteAttribute(cityAttr.id);
      invalidate();
    } else if (!cityAttr && v) {
      add.mutate({ type: "location", value: v });
    }
  }

  // The city/region text only narrows the search at "Local" scope -- once the
  // candidate has widened to National (country-wide) or International (no
  // country filter), the specific city is no longer what the search is keying
  // on, so showing it back to them just reads as a stale/contradictory value.
  // The underlying attribute is untouched (still there for the fail-closed
  // country inference at National scope, see COUNTRY_TOKENS above), so
  // switching back to Local reveals it again exactly as left.
  const showCity = scope === "local";

  return (
    <div className="location-row">
      <div className="location-line">
        {showCity && (
          <input
            className="input"
            style={{ width: 170 }}
            placeholder="City or region…"
            value={city}
            onChange={(e) => setCity(e.target.value)}
            onBlur={persistCity}
            onKeyDown={(e) => e.key === "Enter" && persistCity()}
          />
        )}
        <div className="choice-row">
          {SCOPE_CHOICES.map((s) => (
            <button
              key={s.value}
              className={`toggle ${
                SCOPE_ORDER.indexOf(s.value) <= SCOPE_ORDER.indexOf(scope) ? "on" : "off"
              }`}
              onClick={() => setScope(s.value)}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>
      {showCountryChips && (
        <div className="choice-row" style={{ flexWrap: "wrap", gap: 6 }}>
          {/* Selected countries as removable chips (the list is worldwide now, so
              the picker is a searchable dropdown rather than 250 chips). */}
          {COUNTRY_CHOICES.filter((c) => selectedCountries.has(c.code)).map((c) => (
            <button
              key={c.code}
              className="country on"
              onClick={() => toggleCountry(c.code)}
              title="Remove"
            >
              {c.label} ✕
            </button>
          ))}
          <select
            className="input"
            style={{ width: 200 }}
            value=""
            onChange={(e) => {
              if (e.target.value) toggleCountry(e.target.value);
            }}
          >
            <option value="">+ Add country…</option>
            {COUNTRY_CHOICES.filter((c) => !selectedCountries.has(c.code)).map((c) => (
              <option key={c.code} value={c.code}>
                {c.label}
              </option>
            ))}
          </select>
        </div>
      )}
      {showCountryChips && noneSelected && detected && (
        <div className="hint-detected">
          Detected{" "}
          <strong>{COUNTRY_CHOICES.find((c) => c.code === detected)?.label}</strong>{" "}
          from your location — used automatically unless you pick a country.
        </div>
      )}
    </div>
  );
}
