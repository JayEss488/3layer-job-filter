"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/api";
import { getUsername, setNeedsSurvey } from "@/lib/auth";

/**
 * The two sign-up questions, asked once, on their own page.
 *
 * Deliberately AFTER the account exists rather than on the sign-up form. The
 * whole point of self-serve is that nothing stands between wanting to try the
 * tool and having an account; questions on the form are friction at the exact
 * moment there is least patience for it, and they'd be asked of people who
 * never finish. Here, the account is already made — if someone abandons this
 * page they can come straight back to it.
 *
 * Both answers are required (see providers.tsx for where that's enforced, and
 * why it's enforced in the client rather than the server). "None of these" is a
 * first-class option on Q1 precisely so "required" never means "pick something
 * untrue" — an answer nobody means is worse than no answer, because it looks
 * like signal in GET /admin/signups.
 *
 * The option SLUGS are the contract with the backend
 * (config.SIGNUP_PRIORITY_CHOICES); the labels live here so wording can be
 * reworded freely without invalidating answers already collected.
 */
const PRIORITIES: { slug: string; label: string }[] = [
  { slug: "ghost_roles", label: "Avoiding jobs that aren’t really hiring" },
  { slug: "visa_sponsors", label: "Filtering to visa sponsors" },
  { slug: "faster", label: "Discovering OK roles faster" },
  { slug: "hard_to_find_fit", label: "It’s difficult to find roles that fit" },
  { slug: "niche", label: "Finding niche / lower-competition roles" },
  { slug: "none", label: "None of these" },
];

export default function Welcome() {
  const router = useRouter();
  const [priority, setPriority] = useState<string | null>(null);
  const [usedAi, setUsedAi] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const complete = priority !== null && usedAi !== null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!complete || busy) return;
    setBusy(true);
    setError("");
    try {
      await api.submitSignupSurvey(priority!, usedAi!);
      // Clear the cached gate flag BEFORE navigating, or providers.tsx bounces
      // straight back here and the page looks like it didn't submit.
      setNeedsSurvey(false);
      router.replace("/start");
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  const name = getUsername();

  return (
    <div className="lp lp-plain">
      <div className="lp-sheet lp-narrow">
        <header className="lp-header">
          <div className="lp-brand">
            <div className="lp-mark">4</div>
            <div className="lp-brandname">Four in a Thousand</div>
          </div>
        </header>

        <form className="lp-survey" onSubmit={submit}>
          <div className="lp-survey-lead">
            <h1 className="lp-survey-h1">You&rsquo;re in{name ? `, ${name}` : ""}.</h1>
            <p className="lp-survey-sub">
              Two questions before your first search. They shape what gets built next — nothing
              here changes your results.
            </p>
          </div>

          <fieldset className="lp-q">
            <legend className="lp-q-label">
              <span className="lp-q-n">1</span> Which of these matters most to you right now?
            </legend>
            <div className="lp-options">
              {PRIORITIES.map((p) => (
                <button
                  key={p.slug}
                  type="button"
                  className={`lp-option${priority === p.slug ? " on" : ""}`}
                  onClick={() => setPriority(p.slug)}
                  aria-pressed={priority === p.slug}
                >
                  {p.label}
                </button>
              ))}
            </div>
          </fieldset>

          <fieldset className="lp-q">
            <legend className="lp-q-label">
              <span className="lp-q-n">2</span> Have you used another AI job search tool before?
            </legend>
            <div className="lp-q-hint">Outside general chatbots like ChatGPT or Claude.</div>
            <div className="lp-options lp-options-row">
              <button
                type="button"
                className={`lp-option${usedAi === true ? " on" : ""}`}
                onClick={() => setUsedAi(true)}
                aria-pressed={usedAi === true}
              >
                Yes
              </button>
              <button
                type="button"
                className={`lp-option${usedAi === false ? " on" : ""}`}
                onClick={() => setUsedAi(false)}
                aria-pressed={usedAi === false}
              >
                No
              </button>
            </div>
          </fieldset>

          {error && <div className="lp-signin-error">{error}</div>}

          <button className="btn btn-primary lp-survey-go" type="submit" disabled={!complete || busy}>
            {busy ? (
              <>
                <span className="spinner">◴</span> Saving…
              </>
            ) : (
              "Start using it →"
            )}
          </button>
          {!complete && (
            <div className="lp-join-fine lp-centred">Answer both to continue.</div>
          )}
        </form>
      </div>
    </div>
  );
}
