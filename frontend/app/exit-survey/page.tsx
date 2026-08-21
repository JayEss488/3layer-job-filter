"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { getUsername, setBetaExpired, setNeedsExitSurvey } from "@/lib/auth";

/**
 * The wrap-up survey, and the end-of-beta screen. One route, three states.
 *
 * Both open-beta gates land here (see providers.tsx), because they are
 * independent and can fire together:
 *
 *  - **day 4+, unanswered** — the questions, then straight back into the app.
 *    The user still has days left and should spend them, so this is a
 *    checkpoint, not a goodbye.
 *  - **expired, unanswered** — the questions, then the ended screen. This is
 *    the person who never logged in between day 4 and day 7, and is exactly
 *    who the day-4 trigger exists to catch: asking on the last day only would
 *    have required them to show up on one specific date.
 *  - **expired, answered** — the ended screen alone.
 *
 * Which one is decided from /me + GET /exit/survey, never from the cached
 * flags: those are a paint-time optimisation and can be stale.
 *
 * The option SLUGS are the contract with the backend
 * (config.EXIT_SURVEY_FEATURE_CHOICES / EXIT_SURVEY_SPEED_CHOICES); the labels
 * live here so wording can be reworded freely without invalidating answers
 * already collected — same rule as /welcome.
 */
const FEATURES: { slug: string; label: string }[] = [
  { slug: "ghost_check", label: "Ghost job checking" },
  { slug: "sponsor_check", label: "Visa sponsor checking" },
  { slug: "one_line_summary", label: "The role bullet points" },
  { slug: "why_qualified", label: "The qualification ticklist" },
  { slug: "none", label: "None of these" },
];

const SPEEDS: { slug: string; label: string }[] = [
  { slug: "slower_better", label: "Wait longer for slightly better roles" },
  { slug: "as_is", label: "Keep it as it is" },
  { slug: "faster_worse", label: "Wait a lot less for slightly worse roles" },
];

export default function ExitSurvey() {
  const router = useRouter();

  const [loading, setLoading] = useState(true);
  const [answered, setAnswered] = useState(false);
  const [expired, setExpired] = useState(false);

  const [change, setChange] = useState("");
  const [features, setFeatures] = useState<string[]>([]);
  const [speed, setSpeed] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.me(), api.getExitSurvey()])
      .then(([me, survey]) => {
        if (cancelled) return;
        setExpired(me.beta_expired);
        setAnswered(survey.answered);
        // Keep the caches honest — this page is the one place both flags can
        // legitimately change, and providers.tsx reads them on the next paint.
        setBetaExpired(me.beta_expired);
        setNeedsExitSurvey(me.needs_exit_survey);
        // Nothing to ask and nothing to say: someone typed the URL while their
        // window is still open. Send them back rather than letting them answer
        // early, which would mean they are never asked at day 4.
        if (!me.needs_exit_survey && !me.beta_expired) {
          router.replace("/start");
          return;
        }
        setLoading(false);
      })
      .catch(() => {
        // Never strand the user on a spinner. Showing the form is the safe
        // failure: submitting is idempotent, so a needless ask costs nothing.
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  /** "None of these" is exclusive — ticking it with other options is incoherent. */
  function toggleFeature(slug: string) {
    setFeatures((prev) => {
      if (slug === "none") return prev.includes("none") ? [] : ["none"];
      const without = prev.filter((f) => f !== "none");
      return without.includes(slug) ? without.filter((f) => f !== slug) : [...without, slug];
    });
  }

  // `change` is deliberately absent: a required free-text box on a blocking page
  // is where people either bail or type "n/a", and an answer nobody means looks
  // like signal in the admin readout.
  const complete = features.length > 0 && speed !== null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!complete || busy) return;
    setBusy(true);
    setError("");
    try {
      await api.submitExitSurvey({
        change,
        useful_features: features,
        speed_tradeoff: speed!,
      });
      // Clear the cached gate flag BEFORE navigating, or providers.tsx bounces
      // straight back here and the page looks like it didn't submit.
      setNeedsExitSurvey(false);
      if (expired) {
        setAnswered(true);
        setBusy(false);
      } else {
        router.replace("/start");
      }
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  const name = getUsername();

  if (loading) {
    return (
      <div className="screen">
        <div className="center-pad">
          <span className="spinner">◴</span> Loading…
        </div>
      </div>
    );
  }

  return (
    <div className="lp lp-plain">
      <div className="lp-sheet lp-narrow">
        <header className="lp-header">
          <div className="lp-brand">
            <div className="lp-mark">4</div>
            <div className="lp-brandname">Four in a Thousand</div>
          </div>
        </header>

        {answered ? (
          <div className="lp-survey">
            <div className="lp-survey-lead">
              <h1 className="lp-survey-h1">
                That&rsquo;s the end of your test{name ? `, ${name}` : ""}.
              </h1>
              <p className="lp-survey-sub">
                Thanks for using it, and for the feedback — it&rsquo;s read, and it decides what
                gets built next. Your account and everything in it is kept.
              </p>
            </div>
            <p className="lp-join-fine lp-centred">
              Want more time, or spotted something after this? Reply to the team and we can
              reopen your access.
            </p>
          </div>
        ) : (
          <form className="lp-survey" onSubmit={submit}>
            <div className="lp-survey-lead">
              <h1 className="lp-survey-h1">
                {expired
                  ? "Before you go — three questions."
                  : `Three quick questions${name ? `, ${name}` : ""}.`}
              </h1>
              <p className="lp-survey-sub">
                {expired
                  ? "Your test window has ended. These decide what gets built next."
                  : "You’re a few days in. These decide what gets built next — then you’re straight back to your results."}
              </p>
            </div>

            <fieldset className="lp-q">
              <legend className="lp-q-label">
                <span className="lp-q-n">1</span> What would you change, if you were building
                this as your own personal tool?
              </legend>
              <div className="lp-q-hint">Optional — but it&rsquo;s the most useful box here.</div>
              <textarea
                className="textarea-input"
                style={{ minHeight: 90 }}
                placeholder="Anything at all — what annoyed you, what you'd rip out, what's missing…"
                value={change}
                onChange={(e) => setChange(e.target.value)}
              />
            </fieldset>

            <fieldset className="lp-q">
              <legend className="lp-q-label">
                <span className="lp-q-n">2</span> Which of these materially proved useful?
              </legend>
              <div className="lp-q-hint">Tick any that did.</div>
              <div className="lp-options">
                {FEATURES.map((f) => (
                  <button
                    key={f.slug}
                    type="button"
                    className={`lp-option${features.includes(f.slug) ? " on" : ""}`}
                    onClick={() => toggleFeature(f.slug)}
                    aria-pressed={features.includes(f.slug)}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
            </fieldset>

            <fieldset className="lp-q">
              <legend className="lp-q-label">
                <span className="lp-q-n">3</span> Assuming about 12 roles per run, would you
                rather&hellip;
              </legend>
              <div className="lp-options">
                {SPEEDS.map((s) => (
                  <button
                    key={s.slug}
                    type="button"
                    className={`lp-option${speed === s.slug ? " on" : ""}`}
                    onClick={() => setSpeed(s.slug)}
                    aria-pressed={speed === s.slug}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
            </fieldset>

            {error && <div className="lp-signin-error">{error}</div>}

            <button
              className="btn btn-primary lp-survey-go"
              type="submit"
              disabled={!complete || busy}
            >
              {busy ? (
                <>
                  <span className="spinner">◴</span> Saving…
                </>
              ) : expired ? (
                "Send feedback →"
              ) : (
                "Back to my results →"
              )}
            </button>
            {!complete && (
              <div className="lp-join-fine lp-centred">Answer 2 and 3 to continue.</div>
            )}
          </form>
        )}
      </div>
    </div>
  );
}
