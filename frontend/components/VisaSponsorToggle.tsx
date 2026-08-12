"use client";

import { useState } from "react";

import { useAttributeMutations } from "@/lib/hooks";
import type { Attribute } from "@/lib/types";

// Mirrors backend/app/config.DEFAULT_VISA_SPONSOR_MIN_SALARY.
const DEFAULT_MIN_SALARY = "41700";

// The Skilled Worker route is not one cutoff. GBP 41,700 is the general
// threshold for a standard applicant; several categories are sponsorable well
// below it -- new entrants (under 26, a recent Student/Graduate switcher, or
// training toward a professional qualification) at ~70% of the going rate for
// up to four years, PhD holders, roles on the Immigration Salary List, and a
// specific health/education occupation table. None of those are things this
// app can know about the candidate or detect in a listing, so the floor is
// just a number the candidate can lower to whichever of these applies to them
// -- see backend/app/config.py's visa_sponsor_min_salary entry.
const PRESETS: { value: string; label: string }[] = [
  { value: DEFAULT_MIN_SALARY, label: "£41,700 — standard" },
  { value: "37500", label: "£37,500 — PhD" },
  { value: "33400", label: "£33,400 — new entrant / discounted / STEM PhD" },
  { value: "31300", label: "£31,300 — health & education roles" },
  { value: "0", label: "No floor" },
];

/**
 * Minimum annual salary a role must clear to stay in results while the
 * sponsors-only filter is on. Single-value, same shape as MaxListingAgePicker:
 * at most one visa_sponsor_min_salary row, no row at all means the backend
 * default (£41,700, the standard-applicant rate) applies.
 *
 * Deliberately just a number the candidate can pick or type, not a form asking
 * age/PhD/occupation -- see the PRESETS comment. "No floor" (0) turns the
 * salary check off entirely while leaving the sponsor filter itself on, for a
 * candidate on the Health and Care Worker route or another path these figures
 * don't govern.
 */
function VisaSponsorMinSalaryPicker({
  profileId,
  attributes,
}: {
  profileId: number;
  attributes: Attribute[];
}) {
  const { add, remove } = useAttributeMutations(profileId);
  const current = attributes[0]?.value ?? DEFAULT_MIN_SALARY;

  const options = PRESETS.some((p) => p.value === current)
    ? PRESETS
    : [...PRESETS, { value: current, label: `£${Number(current).toLocaleString("en-GB")} — custom` }];

  function setFloor(value: string) {
    if (current === value) return;
    attributes.forEach((a) => remove.mutate(a.id));
    add.mutate({ type: "visa_sponsor_min_salary", value });
  }

  function setCustom(raw: string) {
    const digits = raw.replace(/[^\d]/g, "");
    if (!digits) return;
    setFloor(String(Math.max(0, parseInt(digits, 10))));
  }

  return (
    <div className="sponsor-salary-floor">
      <div className="choice-row">
        {options.map((p) => (
          <button
            key={p.value}
            className={`toggle ${current === p.value ? "on" : "off"}`}
            onClick={() => setFloor(p.value)}
          >
            {p.label}
          </button>
        ))}
      </div>
      <label className="sponsor-salary-custom">
        Or your own figure:{" "}
        <input
          type="number"
          min={0}
          step={100}
          placeholder="e.g. 35000"
          defaultValue=""
          onBlur={(e) => e.target.value && setCustom(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") setCustom(e.currentTarget.value);
          }}
        />
      </label>
    </div>
  );
}

/**
 * Single-value boolean: at most one visa_sponsor_only row, whose presence with
 * value "true" means on. No row at all means off, which is the default -- see
 * backend/app/config.ATTRIBUTE_TYPES and snapshot._parse_visa_sponsor_only.
 *
 * Deliberately has no Hard/Soft control, unlike every other preference card:
 * there is no useful soft reading of "I need a visa".
 *
 * The notes below the buttons are the only place in the UI that says what this
 * filter can and cannot see, and they should stay. There are now THREE limits,
 * and they fail in different directions:
 *
 *   * the register is EMPLOYER-level, so a licensed employer can still decline
 *     to sponsor any particular vacancy — the reason the expandable panel
 *     exists, because "licensed" reads as "will sponsor me" and doesn't mean it;
 *   * the match is on company NAME, so an agency-posted or vaguely-named role
 *     is excluded even when the end employer does sponsor;
 *   * the salary floor (VisaSponsorMinSalaryPicker) is a single number standing
 *     in for a Skilled Worker rule that isn't actually a single number — see
 *     that component's own comment for the reduced-rate categories it can't
 *     detect and is deliberately left to the candidate to account for.
 *
 * A user who turns this on and sees their result count collapse — or who applies
 * to a "sponsoring" employer and is turned away at the first question — deserves
 * to know all three before concluding the app is broken.
 */
export function VisaSponsorToggle({
  profileId,
  attributes,
  minSalaryAttributes,
}: {
  profileId: number;
  attributes: Attribute[];
  minSalaryAttributes: Attribute[];
}) {
  const { add, remove } = useAttributeMutations(profileId);
  const on = attributes.some((a) => a.value.toLowerCase() === "true");
  const [explain, setExplain] = useState(false);
  const minSalary = minSalaryAttributes[0]?.value ?? DEFAULT_MIN_SALARY;

  function set(next: boolean) {
    if (next === on) return;
    attributes.forEach((a) => remove.mutate(a.id));
    if (next) add.mutate({ type: "visa_sponsor_only", value: "true" });
  }

  return (
    <div className="location-row">
      <div className="choice-row">
        <button className={`toggle ${on ? "on" : "off"}`} onClick={() => set(true)}>
          Sponsors only
        </button>
        <button className={`toggle ${!on ? "on" : "off"}`} onClick={() => set(false)}>
          Any employer
        </button>
      </div>
      {on && (
        <>
          <div className="pref-sublabel">Minimum salary to count as sponsorable</div>
          <VisaSponsorMinSalaryPicker profileId={profileId} attributes={minSalaryAttributes} />
          <div className="hint-detected">
            Hard filter, matched on employer name against the Home Office register.
            Roles advertised by <strong>recruitment agencies</strong>, or with no
            employer named, can&rsquo;t be checked and are excluded &mdash; so expect
            far fewer results. A licensed employer{" "}
            <strong>can still decline to sponsor a particular role</strong>.{" "}
            {minSalary === "0" ? (
              "No minimum salary is set above, so pay isn't used to filter results."
            ) : (
              <>
                Above, a role is also dropped if its stated pay is clearly under{" "}
                <strong>£{Number(minSalary).toLocaleString("en-GB")}/year</strong> &mdash;
                unpriced roles are never penalised for it.
              </>
            )}{" "}
            <button
              type="button"
              className="info-dot"
              aria-expanded={explain}
              aria-label="What sponsorship is and isn't likely to be offered"
              onClick={() => setExplain((v) => !v)}
            >
              i
            </button>
          </div>
          {explain && (
            <div className="info-panel">
              <div className="info-panel-h">What we can and can&rsquo;t tell you</div>
              <p>
                The register says an <strong>employer holds a sponsor licence</strong>. It
                never says which vacancies they will use it on, and that is the question
                you actually have. So we read each listing for what it says itself:
              </p>
              <ul>
                <li>
                  <strong>&ldquo;Sponsorship offered&rdquo;</strong> &mdash; the advert
                  says so in as many words. The strongest thing we can show you, and rare.
                </li>
                <li>
                  <strong>&ldquo;No sponsorship&rdquo;</strong> &mdash; the advert says so.
                  Don&rsquo;t spend an application on it, even if the employer is licensed.
                </li>
                <li>
                  <strong>&ldquo;Employer sponsors visas&rdquo;</strong> &mdash; they hold a
                  licence and the advert is silent. This is the common case and it is a
                  maybe, not a yes.
                </li>
                <li>
                  <strong>No badge</strong> &mdash; we couldn&rsquo;t tell. Usually an
                  agency posting or one with no employer named.
                </li>
              </ul>
              <p>
                Roles <strong>less likely</strong> to be sponsored in practice: pay near
                the Skilled Worker salary threshold (many junior and entry roles sit right
                at it), short contracts and temporary work, small employers who have never
                sponsored before, and anything the advert says needs existing right to
                work. <strong>More likely</strong>: permanent roles at larger employers,
                shortage-occupation and specialist skills, and adverts that raise
                sponsorship themselves.
              </p>
              <p>
                The salary floor above is a standard-applicant number, not a rule that
                applies to everyone equally. You can be sponsored well below it if
                you&rsquo;re a <strong>new entrant</strong> (under 26, a recent
                Student/Graduate visa switcher, or training toward a professional
                qualification), hold a <strong>PhD</strong>, the role is on the{" "}
                <strong>Immigration Salary List</strong>, or it&rsquo;s in a specific
                health/education occupation &mdash; roughly £31,300&ndash;£37,500
                depending which applies. The <strong>Health and Care Worker visa</strong>{" "}
                is a separate route not governed by any of these figures at all. Pick
                the preset above that matches you, or set your own.
              </p>
              <p className="info-panel-fine">
                Our register copy is refreshed periodically, so a licence revoked very
                recently may not show yet. When it matters, ask before you apply &mdash;
                it is a fair question and employers expect it.
              </p>
            </div>
          )}
        </>
      )}
    </div>
  );
}
