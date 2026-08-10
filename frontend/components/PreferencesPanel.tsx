"use client";

import { AllowOverqualifiedToggle } from "@/components/AllowOverqualifiedToggle";
import { HardSoftToggle } from "@/components/HardSoftToggle";
import { LocationPicker, WORK_SET } from "@/components/LocationPicker";
import { MaxListingAgePicker } from "@/components/MaxListingAgePicker";
import { SalarySlider } from "@/components/SalarySlider";
import { SeniorityPicker } from "@/components/SeniorityPicker";
import { VisaSponsorToggle } from "@/components/VisaSponsorToggle";
import { WorkStylePicker } from "@/components/WorkStylePicker";
import { useAttributeMutations, useAttributes } from "@/lib/hooks";
import { enforcementOf } from "@/lib/types";
import type { Attribute, Enforcement } from "@/lib/types";

/**
 * The whole Preferences block — every filter the engine actually enforces.
 *
 * Shared by /dashboard and /onboarding rather than written twice. Onboarding
 * used to carry only three of these (seniority, salary, location), so a user
 * built a profile during onboarding, ran their first search, and only
 * discovered work style, listing age, junior-role tolerance and visa
 * sponsorship existed if they later wandered onto /dashboard — i.e. the
 * settings most likely to make a first search return the wrong thing were the
 * ones a first-time user was never shown. Extracting the block is what makes
 * "onboarding matches the profile page" structurally true instead of two lists
 * that happen to agree until the next edit.
 *
 * Reads its own attributes rather than taking them as props: TanStack dedupes
 * the query to one request however many places mount this, and both call sites
 * already have the same query cached.
 */
export function PreferencesPanel({ profileId }: { profileId: number }) {
  const { data: attrs } = useAttributes(profileId);
  const { update } = useAttributeMutations(profileId);
  const g = attrs?.by_type;

  const locationAttrs = g?.location ?? [];
  const cityAttr = locationAttrs.find((a) => !WORK_SET.has(a.value.toLowerCase()));
  const workTypeAttrs = locationAttrs.filter((a) => WORK_SET.has(a.value.toLowerCase()));
  const seniorityAttrs = g?.seniority ?? [];
  const salaryAttr = g?.salary?.[0];
  const maxListingAgeAttrs = g?.max_listing_age ?? [];
  const visaSponsorAttrs = g?.visa_sponsor_only ?? [];
  const allowOverqualifiedAttrs = g?.allow_overqualified ?? [];

  /**
   * Hard/Soft for a preference is stored per attribute row, but the UI shows one
   * toggle per preference — so a group with several rows (every selected
   * seniority level, every ticked work type) writes the same value to all of
   * them. Reading takes the first row's value: they're only ever set together.
   */
  function groupEnforcement(rows: Attribute[], fallback: Enforcement): Enforcement {
    return rows.length ? enforcementOf(rows[0]) : fallback;
  }
  function setGroupEnforcement(rows: Attribute[], v: Enforcement) {
    rows.forEach((a) => update.mutate({ id: a.id, enforcement: v }));
  }

  const workStyleEnforcement = groupEnforcement(workTypeAttrs, "soft");

  return (
    <div className="pref-list">
      <div className="pref-card">
        <div className="pref-label">Seniority</div>
        <div className="pref-field">
          <SeniorityPicker profileId={profileId} attributes={seniorityAttrs} />
        </div>
        <HardSoftToggle
          value={groupEnforcement(seniorityAttrs, "soft")}
          onChange={(v) => setGroupEnforcement(seniorityAttrs, v)}
          disabled={seniorityAttrs.length === 0}
          disabledReason="Pick a seniority level first — there's nothing to enforce yet."
        />
      </div>

      <div className="pref-card">
        <div className="pref-label">Salary</div>
        <div className="pref-field">
          <SalarySlider profileId={profileId} attribute={salaryAttr} />
        </div>
        <HardSoftToggle
          value={salaryAttr ? enforcementOf(salaryAttr) : "soft"}
          onChange={(v) => salaryAttr && update.mutate({ id: salaryAttr.id, enforcement: v })}
          disabled={!salaryAttr}
          disabledReason="Set a salary range first — there's nothing to enforce yet."
        />
      </div>

      <div className="pref-card">
        <div className="pref-label">Location</div>
        <div className="pref-field">
          <LocationPicker
            profileId={profileId}
            attributes={locationAttrs}
            countryAttributes={g?.country ?? []}
            scopeAttributes={g?.location_scope ?? []}
            commuteAttributes={g?.commute_miles ?? []}
          />
        </div>
        <HardSoftToggle
          value={cityAttr ? enforcementOf(cityAttr) : "hard"}
          onChange={(v) => cityAttr && update.mutate({ id: cityAttr.id, enforcement: v })}
          disabled={!cityAttr}
          disabledReason="Enter a city or region first — there's nothing to enforce yet."
        />
      </div>

      <div className="pref-card">
        <div className="pref-label">Work style</div>
        <div className="pref-field">
          <WorkStylePicker
            profileId={profileId}
            attributes={locationAttrs}
            enforcement={workStyleEnforcement}
          />
        </div>
        <HardSoftToggle
          value={workStyleEnforcement}
          onChange={(v) => setGroupEnforcement(workTypeAttrs, v)}
          disabled={workTypeAttrs.length === 0}
          disabledReason="Pick a work style first — there's nothing to enforce yet."
        />
      </div>

      <div className="pref-card">
        <div className="pref-label">Maximum listing age</div>
        <div className="pref-field">
          <MaxListingAgePicker profileId={profileId} attributes={maxListingAgeAttrs} />
        </div>
        <HardSoftToggle
          value={groupEnforcement(maxListingAgeAttrs, "hard")}
          onChange={(v) => setGroupEnforcement(maxListingAgeAttrs, v)}
          disabled={maxListingAgeAttrs.length === 0}
          disabledReason="Hard by default at 30 days — pick a different limit first to change enforcement."
        />
      </div>

      {/* No HardSoftToggle, for the opposite reason to the visa card below:
          turning this on IS the softening. Two-column field, same as that one. */}
      <div className="pref-card">
        <div className="pref-label">Roles below your level</div>
        <div className="pref-field">
          <AllowOverqualifiedToggle profileId={profileId} attributes={allowOverqualifiedAttrs} />
        </div>
      </div>

      {/* No HardSoftToggle: this filter is inherently hard, so the card
          renders a two-column field instead of the usual three. */}
      <div className="pref-card">
        <div className="pref-label">Visa sponsorship</div>
        <div className="pref-field">
          <VisaSponsorToggle profileId={profileId} attributes={visaSponsorAttrs} />
        </div>
      </div>
    </div>
  );
}
