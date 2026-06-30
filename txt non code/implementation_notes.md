# Job Matching App - Implementation Notes

**Stack:** Next.js (React) frontend, FastAPI (Python) backend, PostgreSQL database.
**Scope:** Single-user prototype, architected so multi-user auth can be added later without a rewrite.

---

## 1. Guiding Principles

Two decisions made now that save pain later:

1. **Build single-user, but pretend there's a `user_id` everywhere.** Every table that holds user data gets a `user_id` column. For the prototype, hardcode it to `1`. When auth arrives, you swap the hardcoded value for the authenticated user's ID and add a users table. No schema migration of existing tables, no query rewrites.

2. **Keep the working search engine isolated.** The search engine already works (though will be improved later). Wrap it behind a single clean function/service boundary so the rest of the app calls it without knowing its internals. Don't refactor it while rebuilding everything else.

---

## 2. Database Schema

Replace the current JSON-in-a-column memory with normalised rows. This is the single most important change - it makes constant additions and edits cheap.

### profiles
A user can have multiple named profiles (the tabs on the dashboard).

```
profiles
  id              SERIAL PRIMARY KEY
  user_id         INT NOT NULL DEFAULT 1
  name            TEXT NOT NULL          -- "Profile 1", "Frontend roles", etc.
  is_active       BOOLEAN DEFAULT true
  created_at      TIMESTAMPTZ DEFAULT now()
  updated_at      TIMESTAMPTZ DEFAULT now()
```

### profile_attributes
The core memory table. Every editable element of a profile is one row. New categories never require schema changes.

```
profile_attributes
  id              SERIAL PRIMARY KEY
  profile_id      INT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE
  type            TEXT NOT NULL          -- see "attribute types" below
  value           TEXT NOT NULL          -- "Software Engineer", "React", "£70k-£90k"
  weight          REAL NOT NULL DEFAULT 1.0
  source          TEXT NOT NULL          -- 'cv_parsed' | 'text_parsed' | 'user_added' | 'engine_inferred'
  confirmed       BOOLEAN DEFAULT false  -- has the user explicitly kept/confirmed this?
  created_at      TIMESTAMPTZ DEFAULT now()
  updated_at      TIMESTAMPTZ DEFAULT now()
```

**Attribute types** (the `type` column - keep this a small controlled vocabulary):

| type            | direction   | examples                                | UI control |
|-----------------|-------------|-----------------------------------------|------------|
| `past_role`     | background  | Software Engineer, Tech Lead            | chips      |
| `skill`         | background  | React, Python, AWS                      | chips      |
| `experience`    | background  | "Led team of 8", "Increased revenue 40%"| chips      |
| `seniority`     | background  | Senior                                  | multi-choice buttons |
| `target_role`   | target      | Engineering Manager, Principal Engineer | chips + AI suggest |
| `salary`        | constraint  | min/max stored as one range             | slider     |
| `location`      | constraint  | London, Remote, Hybrid                  | text + multi-choice |
| `custom`        | constraint  | "Only Series B+ startups"               | free text  |

> **Why separate `past_role` from `target_role`:** these are fundamentally different signals. Past roles describe what the candidate *is*; target roles describe what they *want*. Most job tools conflate them and produce bad matches (offering you sideways moves into your old job). Keeping them as distinct types lets the search engine weight them differently.

### feedback_log
Append-only. Never update or delete rows here. This is the audit trail that lets you recompute weights and is the foundation for any future ML.

```
feedback_log
  id              SERIAL PRIMARY KEY
  profile_id      INT NOT NULL REFERENCES profiles(id)
  role_id         INT REFERENCES roles(id)
  action          TEXT NOT NULL          -- 'tick' | 'cross' | 'ignore' | 'apply'
  created_at      TIMESTAMPTZ DEFAULT now()
```

### roles
Every role the engine has fetched, with its lifecycle state per profile.

```
roles
  id              SERIAL PRIMARY KEY
  profile_id      INT NOT NULL REFERENCES profiles(id)
  external_id     TEXT                   -- dedupe key from source API
  title           TEXT NOT NULL
  company         TEXT
  location        TEXT
  url             TEXT
  tags            JSONB                  -- ["React","Python","Senior"] - display only
  salary_text     TEXT
  fit_rank        INT                    -- 1..N within a search batch
  ai_analysis     TEXT                   -- the expensive-AI justification
  status          TEXT NOT NULL DEFAULT 'new'
                  -- 'new' | 'saved' | 'crossed' | 'ignored'
                  -- | 'applied' | 'deleted'
  application_status TEXT                -- null until applied:
                                         -- 'pending' | 'interview' | 'rejected'
  applied_at      TIMESTAMPTZ
  deadline        TIMESTAMPTZ            -- for filtering out past-deadline roles
  created_at      TIMESTAMPTZ DEFAULT now()
  updated_at      TIMESTAMPTZ DEFAULT now()
```

**Status lifecycle:**
```
new ──tick──► saved ──mark applied──► applied ──► (pending/interview/rejected)
 │                       ▲
 ├──cross──► crossed     │
 │                       │
 └──(unseen)──► ignored ─┘  (saved/applied reachable from ignored too)

saved ──move to ignored──► ignored
ignored ──delete──► deleted (hidden from UI entirely)
```

### Indexes to add
```
CREATE INDEX idx_attr_profile     ON profile_attributes(profile_id);
CREATE INDEX idx_attr_type        ON profile_attributes(profile_id, type);
CREATE INDEX idx_roles_profile    ON roles(profile_id, status);
CREATE INDEX idx_roles_external   ON roles(profile_id, external_id);
CREATE INDEX idx_feedback_profile ON feedback_log(profile_id);
```

---

## 3. Memory Logic (the weight system)

Weights are how feedback turns into better results without retraining anything.

**On tick (save):** for each attribute the role matches, nudge weight up.
**On cross (pass):** for each attribute the role matches, nudge weight down.
**On ignore:** mild negative signal (weaker than a cross).

```python
DELTAS = {"tick": +0.10, "cross": -0.15, "ignore": -0.02}
WEIGHT_MIN, WEIGHT_MAX = 0.1, 2.0

def apply_feedback(profile_id, role, action):
    # 1. log it (append-only)
    insert_feedback(profile_id, role.id, action)
    # 2. find which profile attributes this role matched on
    matched = match_role_to_attributes(profile_id, role)
    # 3. nudge their weights, clamped
    for attr in matched:
        new_w = clamp(attr.weight + DELTAS[action], WEIGHT_MIN, WEIGHT_MAX)
        update_attribute_weight(attr.id, new_w)
```

Key rules:
- **Never clamp to zero.** A crossed role might be rejected for reasons unrelated to a given skill. Floor at 0.1 so an attribute can recover.
- **Weights are invisible to the user.** The dashboard shows values as plain chips. Weight lives only in the database and the engine.
- **"Clear memory"** = reset all weights for the profile to 1.0- keep attributes, reset weights only - safer.

`match_role_to_attributes` can start simple (string/tag overlap between the role and the profile's attribute values) and later upgrade to embedding similarity.

---

## 4. Search Engine Integration

Keep the existing engine. Wrap it behind one service boundary.

### Existing pipeline (unchanged)
```
1. Fetch many roles from various APIs
2. Filter out already-found roles + past-deadline roles
3. Round 1 - eliminate by semantic closeness
4. Round 2 - cheap AI narrows to max 10
5. Fetch full role webpages
6. Expensive AI evaluates + scores + writes justification
```

### What changes around it
The engine needs the profile as input and writes roles as output. Define a clean contract:

**Input to engine:** a profile snapshot built from `profile_attributes` (with weights applied), so higher-weighted attributes influence the semantic and AI steps more strongly.

**Output from engine:** a ranked list. Persist each as a `roles` row with `status='new'`, `fit_rank`, `ai_analysis`, `tags`, `deadline`.

**Dedup filter** queries `roles` for existing `external_id`s for this profile before inserting, so re-running search never shows duplicates. Past-deadline filter checks `deadline < now()`.

Run the search as a **background task** (FastAPI `BackgroundTasks` for the prototype; a real job queue like Celery/RQ later). The frontend kicks off a search, then polls or subscribes for results, because the expensive-AI step is slow.

---

## 5. Backend API (FastAPI)

Single-user prototype - no auth middleware yet, but every endpoint takes/derives `profile_id`. Suggested routes:

### Profiles
```
GET    /profiles                      list profiles (the dashboard tabs)
POST   /profiles                      create new profile
PATCH  /profiles/{id}                 rename / set active
DELETE /profiles/{id}                 delete profile
```

### Profile attributes (memory)
```
GET    /profiles/{id}/attributes      grouped by type for the dashboard
POST   /profiles/{id}/attributes      add one (returns it)
PATCH  /attributes/{attr_id}          edit value (seniority, salary, location)
DELETE /attributes/{attr_id}          remove (the x on a chip)
POST   /profiles/{id}/clear-memory    reset weights (+optional log wipe)
```

### Onboarding / parsing
```
POST   /profiles/{id}/parse-cv        multipart upload -> parsed attributes
POST   /profiles/{id}/parse-text      raw text body -> parsed attributes
POST   /profiles/{id}/suggest         {type, context} -> AI suggestion chips
GET    /profiles/{id}/confidence      -> {score, missing:[...], tip}
```

### Search + roles
```
POST   /profiles/{id}/search          kick off background search
GET    /profiles/{id}/search/status   poll: running | done
GET    /profiles/{id}/roles?status=   list roles by status
POST   /roles/{id}/tick               -> saved + apply feedback
POST   /roles/{id}/cross              -> crossed + apply feedback
POST   /roles/{id}/apply              -> applied (sets applied_at)
PATCH  /roles/{id}/application-status {pending|interview|rejected}
POST   /roles/{id}/move-to-ignored
DELETE /roles/{id}                    hard-hide (status=deleted)
```

### Parsing endpoints detail
`parse-cv` and `parse-text` do the same thing to different inputs:
1. Extract text (for CV: pdfplumber / python-docx; for text: use as-is).
2. Send to an LLM with a structured-output prompt that returns JSON keyed by attribute type, **separating past_role from target_role**, and instructed not to inflate or invent claims.
3. Insert each returned item as a `profile_attributes` row with `source='cv_parsed'`/`'text_parsed'`, `confirmed=false`.
4. Return them so the UI can show removable chips.

---

## 6. Confidence Indicator

Drives the onboarding launch bar and the dashboard. Simple weighted completeness score - not ML.

```python
REQUIRED = {            # weight of each toward "ready to search"
  "target_role": 0.30,
  "skill":       0.25,
  "seniority":   0.15,
  "location":    0.15,
  "salary":      0.10,
  "past_role":   0.05,
}

def confidence(profile_id):
    present = types_present(profile_id)   # set of types with >=1 attribute
    score = sum(w for t, w in REQUIRED.items() if t in present)
    missing = [t for t in REQUIRED if t not in present]
    tip = build_tip(missing)              # "Add salary range and target roles..."
    return {"score": round(score*100), "missing": missing, "tip": tip}
```

---

## 7. Frontend (Next.js) - page/component map

Three top-level sections matching the wireframes, plus the one-time onboarding.

```
/onboarding         one-time profile builder
/search             results cards (the main loop)
/my-roles           saved / ignored / applied tabs
/dashboard          profile tabs + editable memory + launch
```

Shared nav: Search | My Roles | Profile(Dashboard) + persistent "Run New Search" button.

### Key components
```
<ProfileTabs>            switch between profiles (dashboard + nav)
<AttributeRow>           label + chips + add/edit, used in dashboard & onboarding
  <Chip>                 value + x  (delete attribute)
  <ChipAdd>              "+ add" -> inline text input
  <SuggestChip>          dashed AI suggestion -> tap to add
<SeniorityPicker>        multi-choice buttons (edit control)
<SalarySlider>           range slider (edit control)
<LocationPicker>         text input + work-type multi-choice
<RoleCard>               rank, title, company, link, tags, ai_analysis
  <SaveButton> <PassButton>     search + ignored tabs
  <ApplyButton> <DeleteButton>  my-roles
  <ApplicationStatus>           pending/interview/rejected toggle
<ConfidenceBar>          score + tip, onboarding & dashboard
<TrainingBanner>         "ticking and crossing trains your search", dismissible
```

### State / data fetching
- Use React Query (TanStack Query) or SWR for server state - it handles the polling on search status and cache invalidation on tick/cross cleanly.
- On tick/cross, optimistically update the card, fire the mutation, let it reconcile. Card visibly moves (crossed sinks to bottom; saved highlights).
- Onboarding parse results write immediately as `confirmed=false` and flip to `confirmed=true` on "Run first search".

---

Extra detail:
Limit cost by having a max of 5 searches a day on one profile.
On second search, ticked results already saved, crossed results deleted, other results all go to 'ignored'.
All results are cached to avoid repeats.
Then the search can move on to collecting the next set of results.
Once pages are fetched (in expensive AI filtering), save as many details as possible. But don't worry about listings becoming expired at a later date for the prototype.
If the user inputs bad filters:
A lightweight pre-check before triggering the search can estimate harshness cheaply - just look at how many roles the semantic filter step returns before passing anything to the expensive AI. If that number is very low (say under 5), surface a warning rather than continuing.
But also show best-available results anyway, with honest framing.


## 8. Build Order (suggested)

1. **Schema + migrations** - get the tables above into Postgres.
2. **Profiles + attributes CRUD API** - the memory backbone.
3. **Dashboard UI** - prove you can view/add/edit/delete attributes. This is the smallest end-to-end slice.
4. **Onboarding** - CV/text parse -> attributes -> reuse dashboard's AttributeRow components.
5. **Search integration** - wire the existing engine behind `/search`, persist roles.
6. **Search results UI** - cards, tick/cross, feedback -> weight updates.
7. **My Roles** - saved/ignored/applied tabs + application status.
8. **Confidence indicator** - last polish.

Each step is shippable on its own and testable before the next.

---

## 9. Multi-User Upgrade Path (later, not now)

When you're ready:
1. Add a `users` table + auth (NextAuth on the frontend, JWT/session verification in FastAPI).
2. Replace the hardcoded `user_id = 1` with the authenticated user's ID in one place (a dependency in FastAPI).
3. Add `WHERE user_id = :current_user` (via the profile join) to list queries - or enforce with Postgres row-level security.

Because every table already carries `user_id` (directly or through `profiles`), no table needs restructuring. This is the entire payoff of principle #1.