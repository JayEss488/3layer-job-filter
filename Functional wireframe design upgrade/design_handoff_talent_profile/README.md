# Handoff: Talent Profile — Dashboard & app-wide visual system (direction “1a”)

## Overview
A candidate-facing job-matching web app. The user maintains a **Profile** (target roles, past
roles, skills, experience, and search preferences) that powers an automated role search. This
handoff covers the **Profile & Dashboard** screen and, more importantly, the **visual system**
to apply across the rest of the app (Search, My Roles, Settings).

## About the design files
`talent-profile-dashboard.html` is a **design reference created in HTML** — a high-fidelity
prototype of the intended look, not production code to ship as-is. Recreate it in this project’s
existing environment (React/Vue/etc.) using its established component patterns and libraries. If
the project has no front-end yet, pick the most appropriate framework and implement there. The
inline `<style>` block is organised as a **design-token layer** (CSS custom properties) followed
by component classes — mirror that structure with your styling system (CSS vars, Tailwind theme,
styled-components theme, etc.).

## Fidelity
**High-fidelity.** Colors, typography, spacing, and radii are final. Recreate pixel-accurately
using the codebase’s primitives. The only thing intentionally left open is real interactivity
(the prototype is static) — see *Interactions* below for intended behavior.

---

## The visual system (apply to ALL pages)

**Personality:** dense, calm, professional “pro tool” — warm neutral paper, one terracotta
accent, clean geometric sans. No gradients, no heavy shadows, no second accent hue.

**Typography** — Geist (UI) + Geist Mono (reserved for future numeric/labels; not required on 1a).
- Page title `h2`: 19px / 600 / letter-spacing −.02em
- Section header: 12px / 600
- Body & chips: 12.5px / 400–500
- Labels & captions: 12–12.5px / `--muted`
- Sub-labels (e.g. PREFERENCES): 10.5px / 600 / letter-spacing .09em / uppercase
- Big stat number: 30px / 700 / letter-spacing −.03em

**Color tokens** (hex is authoritative — copy verbatim):
- Paper / app bg `#faf8f3` · Card/surface `#ffffff` · Nav & subtle fills `#fdfcf9` · Input/exp rows `#fdfbf6`
- Ink (primary) `#26221c` · Secondary `#5f584c` · Muted labels `#8a8172` · Faint/placeholder `#a89e8c` · Disabled number `#c3bcae`
- Card border `#ece6d9` · Row divider `#f4efe4` · Chip/input border `#e3ddd0` · Ghost dashed `#d6cfbe`
- **Accent (terracotta)** `#bf5c3a` · hover `#a94d2f` · selected-chip tint `#f7f2e9` (border `#e6ddc9`) · “suggest” ghost border `#e3b79f`
- App-outside-frame bg `#e9e5dc`

**Radii:** card 16px · panel 13px · stat card 12px · control/toggle/tab 8px · pill/chip 999px · experience chip 9px.

**Elevation:** cards are flat with 1px borders. The outer app frame only: `0 6px 22px -8px rgba(40,28,14,.14)`. Do not add shadows to inner cards.

**Spacing rhythm:** app frame padding 22–24px · card padding 16–18px · form rows 12px vertical, separated by 1px `--line-2` dividers · chip gaps 6–7px · label column fixed width 96px.

**Iconography:** minimal. Run = `▶` (9px), Upload = `↑`, remove = `✕` at 40% opacity, add = `＋` (dashed ghost), suggest = `✦` in accent. Swap for your icon set (Lucide etc.) at equivalent size; keep them monochrome.

### Reusable components (already in the reference stylesheet)
- **`.btn`** variants: `.btn-primary` (accent fill, white), `.btn-secondary` (white, 1px border), `.btn-ghost` (transparent, muted). Primary hover → `--accent-hover`.
- **Top nav** `.nav` / `.tabs`: text tabs, active tab = ink + 600 + 2px accent underline.
- **Stat card** `.stat`: white card, 30px number, muted label; zero-value number uses `--disabled`.
- **Profile tabs** `.ptab`: active = ink fill; inactive = white outline; “new” = dashed ghost. All dismissible with `✕`.
- **Form row** `.row`: 96px `.label` + flex-wrap `.field`. `.field.col` stacks (used for Experience).
- **Chip** `.chip` (white/outline, removable) · `.chip.tinted` (accent tint = target role) · `.chip.solid` (past roles) · `.chip.exp` (full-width experience bullet). Ghosts: `.ghost`, `.ghost.suggest`.
- **Toggle pill** `.toggle`: `.on` = accent fill, `.off` = white outline. Used for Seniority multi-select and Work-mode single-select.
- **Slider** `.track`/`.rail`/`.fill`/`.knob`: accent fill + white knob with accent ring; min/max labels under.
- **Input** `.input`: warm off-white field, faint placeholder.

### Applying it to the other pages
Keep the same frame, nav, type scale, tokens, and component classes. Guidance per page:
- **Search** — results list of role cards inside the same `.panel` shell; each card uses the chip/toggle vocabulary for match tags; primary action = accent `.btn-primary`. Ticking/crossing a result reuses the chip `✕`/`＋` affordance.
- **My Roles** — saved/applied roles as `.stat`-style summary + a table or card list using the same borders (`--line`) and radii; status pills reuse `.toggle`/`.chip` styles.
- **Settings** — the same `.row` label+field pattern (96px label column) for grouped settings; toggles reuse `.toggle` on/off; destructive actions use `.btn-ghost`.

Consistency rules: one accent only; flat bordered cards; 96px label column on any form; accent reserved for primary action + selected state; muted grey for everything secondary.

---

## Screen: Profile & Dashboard

**Purpose:** the candidate reviews search stats and edits the profile that drives matching.

**Layout (top → bottom), 820px frame:**
1. **Top nav** — logo mark + 4 text tabs (Search / My Roles / **Profile** active / Settings) on the left; **Run New Search** primary button right.
2. **Header** — “Profile & Dashboard” title + “Updated 2 hours ago” caption.
3. **Stats** — 3 equal cards: `9 Roles searched`, `1 Saved`, `0 Applied` (zero greyed).
4. **Profile tabs** — `priya` (active, ink) · `Profile 2` (outline) · `＋ new` (dashed). Each removable.
5. **“What you’re looking for” panel** — form rows:
   - Target roles — 1 tinted chip + add + suggest
   - Past roles — 4 solid chips + add
   - Skills — 12 outline chips + add + suggest
   - Experience — 7 full-width bullet chips + add
   - **PREFERENCES** sub-label
   - Seniority — 6 toggles (Junior/Mid/Senior on; Lead/Director/C-Suite off)
   - Salary — slider, £70k–£98k, knob ~44%
   - Location — city input + On-site/Hybrid/**Remote**(on) + 14 country chips (all unselected)
   - Anything else — add ghost
6. **Footer** — Run New Search (primary) + Upload new CV (secondary) left; Clear memory (ghost) right; italic helper note below.

Exact copy, colors, and per-component specs live in `talent-profile-dashboard.html` — treat it as source of truth.

## Interactions & behavior (intended; prototype is static)
- **Chips** are removable (`✕`) and addable (`＋ add`). `✦ suggest` requests AI suggestions for that field.
- **Seniority** = multi-select toggles; **Work mode** (On-site/Hybrid/Remote) and **Country** = selectable filters (country chips toggle to the accent-fill `.on` state when selected).
- **Salary** = range slider (currently single knob shown; confirm single vs. dual-handle range).
- **Run New Search** runs the search from the current profile; **Upload new CV** re-parses experience/skills; **Clear memory** resets learned weights.
- Per the footer note: ticking/crossing roles in **Search** feeds back into these profile weights automatically.
- Hover: `.btn-primary` darkens to `--accent-hover`; chips should show a subtle hover (e.g. border → `--muted`) and reveal the `✕` — apply consistently.

## State (suggested)
- `profiles[]` with active id; each profile holds `targetRoles[], pastRoles[], skills[], experience[], seniority[], salaryRange, workMode, countries[], notes[]`.
- `stats { searched, saved, applied }` derived from search activity.

## Assets
- **Fonts:** Geist + Geist Mono (Google Fonts). Swap for your bundled copies if self-hosting.
- **Icons:** currently Unicode glyphs; replace with your icon library at matching sizes.
- No images/logos — the `m` logo mark is a placeholder; substitute the real brand mark.

## Files in this bundle
- `talent-profile-dashboard.html` — high-fidelity static reference (tokens + components + full screen).
- `README.md` — this document (self-sufficient spec).
