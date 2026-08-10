"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { AppleSignInButton } from "@/components/AppleSignInButton";
import { EmailSignUpForm } from "@/components/EmailSignUpForm";
import { GoogleSignInButton } from "@/components/GoogleSignInButton";
import type { AuthResult } from "@/lib/api";
import { getToken, postSignInRoute, setAuth } from "@/lib/auth";

/**
 * The homepage: public landing page + self-serve sign-up, and the app's entry
 * point for an already-signed-in user.
 *
 * It replaced both the old /login screen (hand-assigned beta credentials) and
 * a rebuild of the standalone marketing page that used to live outside this
 * repo as a static index.html.
 *
 * Layout note: this is the one lp- page that isn't a boxed form. /welcome and
 * /login stay inside .lp-sheet (a centred, bordered card - right for a short
 * form) and are untouched by anything below. This page instead runs full
 * bleed (.lp-home breaks out of body's centred flex box; see globals.css) as
 * a sequence of alternating-background .lp-home-band sections, each
 * re-centring its own content at a wider inner width. Two reasons: a boxed
 * card floating on a page background reads as "app screenshot," not
 * marketing page, and it wastes the sides of a wide viewport doing nothing -
 * neither problem exists once the content owns the row it's on.
 *
 * Content structure leads with ONE differentiator (ghost-listing detection -
 * nothing in the competitor research this was built from does this) rather
 * than eight roughly-equal feature bullets. The old feature grid is now a
 * five-item stat strip; the old "why others fail" reasons grid and the
 * separate "we tested the leading matcher" receipt are merged into one
 * us-vs-them comparison table, so the receipt's real evidence sits directly
 * above the systematic claim it's an example of, instead of two stacked
 * sections making adjacent points.
 *
 * Two branches:
 *   - token present  -> straight to /start, which routes to onboarding or
 *                       search depending on whether the profile has data.
 *   - no token       -> the landing page below, whose call to action is the
 *                       three sign-up controls in #join.
 *
 * There is no waiting list and no approval step. Sign in, answer two questions,
 * you're in the product.
 */
export default function Home() {
  const router = useRouter();
  // `null` = we haven't checked localStorage yet (it doesn't exist during SSR),
  // so neither branch is committed to and the landing page can't flash for a
  // signed-in user.
  const [signedIn, setSignedIn] = useState<boolean | null>(null);

  useEffect(() => {
    if (getToken()) {
      setSignedIn(true);
      router.replace("/start");
    } else {
      setSignedIn(false);
    }
  }, [router]);

  const onSignedIn = useCallback(
    (r: AuthResult) => {
      setAuth(r.token, r.username, r.needs_survey, r.needs_exit_survey, r.beta_expired);
      router.replace(postSignInRoute(r));
    },
    [router]
  );

  if (signedIn !== false) {
    return (
      <div className="screen">
        <div className="center-pad">
          <span className="spinner">◴</span> Loading…
        </div>
      </div>
    );
  }

  return (
    <div className="lp-home">
      {/* ── header ─────────────────────────────────────────────────── */}
      <div className="lp-home-band">
        <header className="lp-header">
          <div className="lp-brand">
            <div className="lp-mark">4</div>
            <div className="lp-brandname">Four in a Thousand</div>
          </div>
          <a href="#join" className="lp-headerlink">
            Join the beta
          </a>
        </header>
      </div>

      {/* ── hero ───────────────────────────────────────────────────── */}
      <div className="lp-home-band">
        <div className="lp-home-inner">
          <section className="lp-hero">
            <div>
              <h1 className="lp-h1">
                Most job tools help you apply to <span className="lp-accent">everything.</span>
                <br />
                We find the few roles that fit.
              </h1>
              <p className="lp-lede">
                We read the whole listing the way a person would - for 480 roles. Then we flag
                potential ghost jobs, check UK visa sponsorship, and hand you the best ones.
              </p>

              {/* Three ways in, side by side. Google alone silently lost
                  everyone without one — and lost them at the FIRST screen, so
                  they never reached a page that could have counted them.
                  Apple and the email form each render themselves as nothing
                  when the server reports that method unconfigured, so this
                  block degrades to exactly what it was before. */}
              <div id="join" className="lp-join">
                <div className="lp-join-h">Join the beta</div>
                <p className="lp-join-sub">
                  Free while we&rsquo;re testing. Sign in and you&rsquo;re straight in.
                </p>
                <GoogleSignInButton onSuccess={onSignedIn} />
                <AppleSignInButton onSuccess={onSignedIn} />
                <EmailSignUpForm onSuccess={onSignedIn} />
                <div className="lp-join-fine">
                  Two quick questions after you sign in, then you can run your first search. This
                  is a beta: it&rsquo;s rough in places, and your feedback changes what gets built
                  next.
                </div>
              </div>
            </div>

            {/* ── the demo card ─────────────────────────────────────────
                Built from the app's own .card / .tag / .verdict styles rather
                than bespoke landing-page markup, so what a visitor is shown here
                is literally the component they get after signing in. If the real
                card changes, this changes with it. */}
            <div>
              <div className="lp-eyebrow">What you&rsquo;ll actually see</div>
              <div className="lp-cardwrap">
                <div className="card">
                  <div className="card-top">
                    <div className="card-main">
                      <div className="card-title">Junior Credit Reporting Analyst</div>
                      <div className="card-company">Redbridge Credit Services - Leeds</div>
                      <div className="card-tags">
                        <span className="tag">up to £29,520</span>
                        <span className="tag">Hybrid</span>
                        <span className="tag">Junior</span>
                      </div>
                    </div>
                    <div className="card-corner">
                      <span className="verdict v-strong">Strong fit</span>
                    </div>
                  </div>

                  <div className="card-headline flush">
                    This is a junior reporting and analysis role. You would query debt-portfolio
                    data, analyse it in spreadsheets and build Power BI reports.
                  </div>

                  <div className="card-analysis flush">
                    <div className="an-note">Matched via: BI/Data Analyst track</div>
                    <div className="an-sec">
                      <div className="an-h">Qualification</div>
                      <div>
                        ✓ You&rsquo;re well qualified for this junior role: your SQL, Excel and
                        Power BI case-study work maps directly to its day-to-day reporting tasks,
                        with the main caveat that your SQL authorship was AI-assisted.
                      </div>
                    </div>
                    <span className="lp-fauxbtn">Show more</span>
                  </div>

                  <div className="card-actions flush">
                    <span className="btn btn-secondary lp-static">↗ View role</span>
                    <span className="btn btn-secondary lp-static">✓ Save</span>
                  </div>
                </div>
              </div>
              <div className="lp-caption">A real result, with the company name changed.</div>
            </div>
          </section>
        </div>
      </div>

      {/* ── what one run does (stat strip) ────────────────────────────── */}
      <div className="lp-home-band alt">
        <div className="lp-home-inner tight">
          <div className="lp-bandsub">What one run actually does</div>
          <div className="lp-stats">
            {STATS_RUN.map((s) => (
              <div className="lp-stat" key={s.l}>
                <div className="lp-stat-n">{s.n}</div>
                <div className="lp-stat-l">{s.l}</div>
              </div>
            ))}
          </div>
          {/* Own grid, not a continuation of the row above: each grid resets
              its own first-child (so the left border resets cleanly instead
              of an item that isn't first in the array still carrying one
              because it's first in a wrapped row), and two items alone wrap
              to a single column on narrow viewports instead of the previous
              5-item auto-fit occasionally leaving an orphaned slot. */}
          <div className="lp-stats lp-stats-checks">
            {STATS_CHECKS.map((s) => (
              <div className="lp-stat" key={s.l}>
                <div className="lp-stat-n">{s.n}</div>
                <div className="lp-stat-l">{s.l}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ── the receipt + us vs. them ──────────────────────────────────── */}
      <div className="lp-home-band">
        <div className="lp-home-inner">
          <div className="lp-proof-grid">
            <div>
              <div className="lp-eyebrow lp-eyebrow-accent">We tested the leading matcher</div>
              <h2 className="lp-h2 lp-h2-tight">It sent a marketing candidate a shop-assistant job.</h2>
              <p className="lp-body">
                The best match it found was a retail assistant role. Elsewhere it surfaced an
                apprenticeship for a graduate profile. In a sample, only{" "}
                <strong>1 in 3 matches was a genuine fit</strong>.
              </p>
            </div>
            <div className="lp-receipt">
              <div className="lp-receipt-h">THEIR &ldquo;TOP MATCH&rdquo;</div>
              <div className="lp-receipt-title">Retail Sales Assistant</div>
              <div className="lp-receipt-sub">for a candidate with a Marketing CV</div>
              <div className="lp-receipt-x lp-receipt-first">
                <span>✕</span>
                <span>No overlap with the candidate&rsquo;s field or experience</span>
              </div>
              <div className="lp-receipt-x">
                <span>✕</span>
                <span>No explanation of why it was matched at all</span>
              </div>
            </div>
          </div>

          <div className="lp-compare-lead">
            <h2 className="lp-h2">Why the tools you&rsquo;ve tried don&rsquo;t solve this</h2>
          </div>
          <div className="lp-compare">
            <div className="lp-compare-head">
              <div className="them">Most job-matching tools</div>
              <div className="us">Four in a Thousand</div>
            </div>
            {COMPARE.map((row) => (
              <div className="lp-compare-row" key={row.them}>
                <div className="them">
                  <span>✕</span>
                  {row.them}
                </div>
                <div className="us">
                  <span>✓</span>
                  {row.us}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ── CTA ────────────────────────────────────────────────────────── */}
      <div className="lp-home-band alt">
        <div className="lp-home-inner tight">
          <div className="lp-cta">
            <div className="lp-cta-text">
              Job searching already feels like a full-time unpaid job.
              <span className="lp-cta-sub">Spend the five minutes here instead.</span>
            </div>
            <a href="#join" className="lp-cta-btn">
              Join the beta
            </a>
          </div>
        </div>
      </div>

      <div className="lp-home-band">
        <footer className="lp-footer">
          <span>© 2026 Four in a Thousand</span>
          <a href="/login" className="lp-footlink">
            Beta tester with a username?
          </a>
        </footer>
      </div>
    </div>
  );
}

/** The stat strip that replaced the old 8-item feature grid. Same underlying
 *  claims, but picking five and stating them as facts (verifiable against the
 *  demo card above) reads as evidence rather than a features list - and
 *  leaves the two more involved claims (match depth, coverage) to the
 *  comparison table below, where there's room to say them properly instead
 *  of squeezing them into a stat.
 *
 *  Split into two arrays, rendered as two separate grids, rather than one
 *  five-item auto-fit grid: the three run-facts (480/3/12) are one kind of
 *  claim and the two checks performed (ghost, sponsor) are another, and
 *  wrapping five items in one grid left an orphaned slot on medium
 *  viewports with a stray border on whichever item happened to wrap into
 *  column one. Each stated once here - the near-identical lines that used
 *  to sit under the demo card as well were cut. */
const STATS_RUN: { n: string; l: string }[] = [
  { n: "480", l: "listings read, every run" },
  { n: "3", l: "layers of AI narrowing them down" },
  { n: "12", l: "shortlisted, ranked, reasoning attached" },
];
const STATS_CHECKS: { n: string; l: string }[] = [
  { n: "Flagged", l: "ghost listings - before you apply, not after" },
  { n: "Checked", l: "every UK role, against the Home Office sponsor register" },
];

/** Merges the old REASONS grid (kept because it tested well - see git
 *  history) with the leftover feature claims that don't fit the stat strip,
 *  paired row-for-row against what we do instead. Row 3 (ghost listings) is
 *  the wedge claim from the hero, restated as the specific contrast it is. */
const COMPARE: { them: string; us: string }[] = [
  {
    them: "Applying to as many places as possible",
    us: "The dozen roles actually worth your time",
  },
  {
    them: "Title, location, experience band - surface only",
    us: "The full listing, read by three layers of AI, with the reasoning shown",
  },
  {
    them: "Ghost listings aren't checked - you find out after you've applied",
    us: "Ghost listings flagged before you apply, with the specific reason",
  },
  {
    them: "Mostly job boards",
    us: "Job boards and the roles employers post on their own site",
  },
  {
    them: "Not checked, or US-only",
    us: "Every UK role checked against the Home Office sponsor register",
  },
];
