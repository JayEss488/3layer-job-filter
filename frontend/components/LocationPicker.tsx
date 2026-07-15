"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
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

const COUNTRY_CHOICES: { code: string; label: string }[] = [
  { code: "global", label: "Global (no filter)" },
  { code: "gb", label: "United Kingdom" },
  { code: "us", label: "United States" },
  { code: "ca", label: "Canada" },
  { code: "au", label: "Australia" },
  { code: "de", label: "Germany" },
  { code: "fr", label: "France" },
  { code: "in", label: "India" },
  { code: "it", label: "Italy" },
  { code: "nl", label: "Netherlands" },
  { code: "at", label: "Austria" },
  { code: "pl", label: "Poland" },
  { code: "sg", label: "Singapore" },
  { code: "za", label: "South Africa" },
];

// Mirrors full_auto.py _COUNTRY_TOKENS (high-signal tokens only) so the picker
// can auto-detect the country from a typed city/region and highlight it — which
// matches the backend's fail-closed default (no explicit country chip -> filter
// by the country inferred from the location).
const COUNTRY_TOKENS: Record<string, string[]> = {
  gb: ["united kingdom", "uk", "u.k.", "great britain", "england", "scotland", "wales",
       "northern ireland", "london", "manchester", "birmingham", "leeds", "glasgow",
       "edinburgh", "bristol", "liverpool", "sheffield", "newcastle", "nottingham",
       "leicester", "coventry", "cardiff", "belfast", "cambridge", "oxford", "reading",
       "brighton", "aberdeen", "dundee", "southampton", "portsmouth", "essex", "kent",
       "surrey", "sussex", "hampshire", "yorkshire", "lancashire", "cheshire", "devon",
       "cornwall", "southend"],
  us: ["united states", "usa", "u.s.", "america", "new york", "san francisco",
       "los angeles", "chicago", "seattle", "austin", "boston", "texas", "california",
       "florida", "washington", "denver", "atlanta", "dallas", "houston", "philadelphia"],
  ca: ["canada", "toronto", "vancouver", "montreal", "ottawa", "calgary"],
  au: ["australia", "sydney", "melbourne", "brisbane", "perth"],
  de: ["germany", "deutschland", "berlin", "munich", "hamburg", "frankfurt"],
  fr: ["france", "paris", "lyon", "marseille"],
  in: ["india", "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "pune"],
  it: ["italy", "italia", "rome", "milan", "turin"],
  nl: ["netherlands", "holland", "amsterdam", "rotterdam", "the hague"],
  at: ["austria", "vienna"],
  pl: ["poland", "warsaw", "krakow", "wroclaw"],
  sg: ["singapore"],
  za: ["south africa", "johannesburg", "cape town", "pretoria"],
};

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

  return (
    <div className="location-row">
      <div className="location-line">
        <input
          className="input"
          style={{ width: 170 }}
          placeholder="City or region…"
          value={city}
          onChange={(e) => setCity(e.target.value)}
          onBlur={persistCity}
          onKeyDown={(e) => e.key === "Enter" && persistCity()}
        />
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
        <div className="choice-row" style={{ flexWrap: "wrap" }}>
          {COUNTRY_CHOICES.map((c) => (
            <button
              key={c.code}
              className={`country${selectedCountries.has(c.code) ? " on" : ""}${
                noneSelected && c.code === detected ? " detected" : ""
              }`}
              onClick={() => toggleCountry(c.code)}
            >
              {c.label}
            </button>
          ))}
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
