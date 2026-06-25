import json
import queue
import threading
import uuid
import base64
import io
import logging
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()
from full_auto import run_pipeline, build_profile, run_search, get_profile_status, save_card_memory


app = Flask(__name__)

# ── Debug logger ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('jobscope')

def _mem_summary(mem: dict) -> str:
    """One-line summary of MEM dict for logging."""
    parts = []
    for section in ['pref', 'titles', 'skills']:
        m = mem.get(section, {})
        parts.append(f"{section}(yes={len(m.get('yes',[]))}, no={len(m.get('no',[]))})")
    return ' | '.join(parts)

# ── Job store ─────────────────────────────────────────────────────────────────
# Keyed by job_id. Each entry: { queue, results, error, done }
JOBS: dict[str, dict] = {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/run", methods=["POST"])
def start_run():
    cv_text = request.json.get("cv_text", "").strip()
    if not cv_text:
        return jsonify({"error": "No CV text provided"}), 400

    job_id = str(uuid.uuid4())
    log_queue = queue.Queue()
    JOBS[job_id] = {"queue": log_queue, "results": None, "error": None, "done": False}

    def worker():
        try:
            results = run_pipeline(cv_text, log_queue)
            JOBS[job_id]["results"] = results
        except Exception as e:
            JOBS[job_id]["error"] = str(e)
            log_queue.put(f"[error] {e}")
        finally:
            JOBS[job_id]["done"] = True
            log_queue.put("__DONE__")  # sentinel

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/stream/<job_id>")
def stream(job_id):
    """Server-Sent Events endpoint. Streams log lines until pipeline finishes."""
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job ID"}), 404

    def generate():
        q = job["queue"]
        while True:
            try:
                msg = q.get(timeout=60)  # 60s timeout per message
            except queue.Empty:
                yield "data: [timeout waiting for update]\n\n"
                break
            if msg == "__DONE__":
                yield "data: __DONE__\n\n"
                break
            # SSE format: each message is "data: ...\n\n"
            yield f"data: {msg}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # important for nginx if you ever deploy
        }
    )


@app.route("/api/results/<job_id>")
def get_results(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job ID"}), 404
    if not job["done"]:
        return jsonify({"status": "running"}), 202
    if job["error"]:
        return jsonify({"error": job["error"]}), 500
    return jsonify({"selections": job["results"]})

@app.route("/results")
def results_page():
    return render_template("job_results_viewer.html")


@app.route("/api/profile_status")
def profile_status():
    status = get_profile_status()
    # Also attach the cached profile if present so the UI can render it
    if status.get("has_profile"):
        try:
            from full_auto import get_db
            conn = get_db()
            row = conn.execute("SELECT value FROM profile_cache WHERE key='profile'").fetchone()
            conn.close()
            if row:
                import json as _json
                profile = _json.loads(row["value"])
                status["profile"] = profile
                log.debug('[profile_status] returning profile: location=%s seniority=%s skills=%d terms=%d',
                          profile.get('location'), profile.get('seniority'),
                          len(profile.get('key_skills', [])), len(profile.get('search_terms', [])))
            else:
                log.warning('[profile_status] has_profile=True but DB row was empty')
        except Exception as e:
            log.error('[profile_status] error reading profile from DB: %s', e)
    else:
        log.debug('[profile_status] no profile in DB — returning status only: %s', status)
    return jsonify(status)


@app.route("/api/build_profile", methods=["POST"])
def start_build_profile():
    data = request.json or {}
    cv_text = data.get("cv_text", "").strip()
    filename = data.get("filename", "")
    input_method = "text" if cv_text else ("base64:" + os.path.splitext(filename)[1].lower() if data.get("base64") else "none")
    log.debug('[build_profile] request received — method=%s filename=%r base64_len=%s cv_text_len=%s',
              input_method, filename,
              len(data["base64"]) if data.get("base64") else 0,
              len(cv_text))

    if not cv_text and data.get("base64"):
        try:
            raw = base64.b64decode(data["base64"])
            ext = os.path.splitext(filename)[1].lower()
            log.debug('[build_profile] decoding binary file — ext=%s raw_bytes=%d', ext, len(raw))

            if ext == ".docx":
                from docx import Document
                doc = Document(io.BytesIO(raw))
                cv_text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
                log.debug('[build_profile] docx extracted — %d chars', len(cv_text))

            elif ext == ".pdf":
                import pdfplumber
                with pdfplumber.open(io.BytesIO(raw)) as pdf:
                    cv_text = "\n".join(
                        page.extract_text() or "" for page in pdf.pages
                    ).strip()
                log.debug('[build_profile] pdf extracted — %d chars', len(cv_text))

            else:
                cv_text = raw.decode("utf-8", errors="replace")
                log.debug('[build_profile] unknown ext, decoded as utf-8 — %d chars', len(cv_text))

        except Exception as e:
            log.error('[build_profile] ❌ file extraction failed: %s', e)
            return jsonify({"error": f"Could not extract text from file: {e}"}), 400

    if not cv_text:
        log.warning('[build_profile] ❌ no CV text after extraction — returning 400')
        return jsonify({"error": "No CV text provided"}), 400

    log.debug('[build_profile] CV text ready — %d chars, spawning worker thread', len(cv_text))
    job_id = str(uuid.uuid4())
    log_queue = queue.Queue()
    JOBS[job_id] = {"queue": log_queue, "results": None, "error": None, "done": False}

    def worker():
        log.debug('[build_profile worker:%s] thread started', job_id[:8])
        try:
            profile = build_profile(cv_text, log_queue)
            JOBS[job_id]["results"] = profile
            log.debug('[build_profile worker:%s] ✓ done — location=%s skills=%d terms=%d',
                      job_id[:8], profile.get('location'),
                      len(profile.get('key_skills', [])),
                      len(profile.get('search_terms', [])))
        except Exception as e:
            log.error('[build_profile worker:%s] ❌ exception: %s', job_id[:8], e)
            JOBS[job_id]["error"] = str(e)
            log_queue.put(f"[error] {e}")
        finally:
            JOBS[job_id]["done"] = True
            log_queue.put("__DONE__")
            log.debug('[build_profile worker:%s] sentinel sent', job_id[:8])

    threading.Thread(target=worker, daemon=True).start()
    log.debug('[build_profile] job_id=%s spawned', job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/run_search", methods=["POST"])
def start_run_search():
    job_id = str(uuid.uuid4())
    log_queue = queue.Queue()
    JOBS[job_id] = {"queue": log_queue, "results": None, "error": None, "done": False}

    def worker():
        try:
            results = run_search(log_queue)
            JOBS[job_id]["results"] = results
        except Exception as e:
            JOBS[job_id]["error"] = str(e)
            log_queue.put(f"[error] {e}")
        finally:
            JOBS[job_id]["done"] = True
            log_queue.put("__DONE__")

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/profile", methods=["PATCH"])
def patch_profile():
    """Add or remove a single item from a list field in the cached profile."""
    data = request.json or {}
    field  = data.get("field")
    value  = data.get("value")
    action = data.get("action", "remove")   # "add" or "remove"
    log.debug('[patch_profile] action=%s field=%s value=%r', action, field, value)
    if not field or value is None:
        log.warning('[patch_profile] missing field or value')
        return jsonify({"error": "field and value required"}), 400
    try:
        from full_auto import get_db
        import json as _json
        conn = get_db()
        row = conn.execute("SELECT value FROM profile_cache WHERE key='profile'").fetchone()
        if not row:
            log.warning('[patch_profile] no profile in DB to patch')
            return jsonify({"error": "No profile cached"}), 404
        profile = _json.loads(row["value"])
        lst = profile.get(field, [])
        before_len = len(lst) if isinstance(lst, list) else None
        if isinstance(lst, list):
            if action == "add" and value not in lst:
                lst.append(value)
            elif action == "remove":
                lst = [v for v in lst if v != value]
            profile[field] = lst
        after_len = len(profile.get(field, []))
        log.debug('[patch_profile] ✓ %s [%s] "%s" — %s→%s items', action, field, value, before_len, after_len)
        conn.execute("INSERT OR REPLACE INTO profile_cache(key,value) VALUES(?,?)",
                     ("profile", _json.dumps(profile)))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        log.error('[patch_profile] ❌ error: %s', e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate_cards", methods=["POST"])
def generate_cards():
    """Use the LLM to generate tailored card prompts for a given mode."""
    import json as _json
    data        = request.json or {}
    mode        = data.get("mode", "pref")
    profile     = data.get("profile") or {}
    already     = data.get("already_seen", [])

    mode_meta = {
        "pref": {
            "label": "work preferences",
            "instruction": (
                "Generate 20 work-preference statements the user can swipe yes/no on. "
                "Each should be a short first-person statement about working style, environment, "
                "sector, pace, values, or salary — e.g. 'I want a fully remote role', "
                "'I prefer a small team under 20 people'. "
                "Tailor them to the candidate's background and location. "
                "Do not repeat any of the already-seen items."
            ),
        },
        "titles": {
            "label": "job titles",
            "instruction": (
                "Generate 20 specific job title strings the user can swipe yes/no on. "
                "Each should be a realistic job board search term — e.g. 'Data Analyst', "
                "'Climate Policy Officer', 'Junior Python Developer'. "
                "Tailor them tightly to the candidate's sectors, seniority, and key skills. "
                "Do not repeat any of the already-seen items."
            ),
        },
        "skills": {
            "label": "skills and experience",
            "instruction": (
                "Generate 20 skill or experience statements the user can confirm yes/no. "
                "Each should start with 'I' and be concrete — e.g. 'I have built REST APIs', "
                "'I have managed a team of 5+', 'I am proficient in SQL'. "
                "Tailor them to the candidate's background. "
                "Do not repeat any of the already-seen items."
            ),
        },
    }

    meta = mode_meta.get(mode, mode_meta["pref"])
    profile_summary = (
        f"Sectors: {', '.join(profile.get('sectors', []))}\n"
        f"Seniority: {profile.get('seniority', 'unknown')}\n"
        f"Key skills: {', '.join(profile.get('key_skills', []))}\n"
        f"Location: {profile.get('location', 'unknown')}\n"
        f"Search terms: {', '.join(profile.get('search_terms', []))}"
    ) if profile else "No profile loaded — generate generic cards."

    prompt = f"""You are building a job-search profile for a candidate.

Candidate profile:
{profile_summary}

Already-seen items (do NOT repeat these):
{json.dumps(already)}

Task: {meta['instruction']}

Output ONLY a JSON object with a single key "cards" containing a list of exactly 20 strings. No markdown, no explanation."""

    try:
        from full_auto import llm
        raw = llm(prompt, require_json=True)
        import json as _json
        cards = _json.loads(raw).get("cards", [])
        # Filter out duplicates with already-seen
        seen_set = set(already)
        cards = [c for c in cards if c not in seen_set]
        return jsonify({"cards": cards})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cleanup_cards", methods=["POST"])
def cleanup_cards():
    """After every batch of 5, use LLM to deduplicate and resolve conflicts in card memory."""
    import json as _json
    data    = request.json or {}
    mode    = data.get("mode", "pref")
    mem     = data.get("mem", {"yes": [], "no": []})
    profile = data.get("profile") or {}
    log.debug('[cleanup_cards] mode=%s | yes=%d no=%d', mode, len(mem.get('yes',[])), len(mem.get('no',[])))

    prompt = f"""You are cleaning up a user's job-search profile preferences.

Mode: {mode} ({"work preferences" if mode=="pref" else "job titles" if mode=="titles" else "skills/experience"})

The user has swiped yes/no on cards. Clean up the results:
- Remove duplicates or near-duplicates (keep the most specific version)
- If yes and no lists conflict (same concept in both), remove from "no" — the more recent "yes" takes priority
- Remove anything clearly irrelevant to the candidate's profile
- Return at most 30 items in "yes" and 30 in "no"

Current yes list: {json.dumps(mem.get("yes", []))}
Current no list:  {json.dumps(mem.get("no", []))}

Candidate profile summary:
Sectors: {', '.join(profile.get('sectors', []))}
Seniority: {profile.get('seniority', '')}
Skills: {', '.join(profile.get('key_skills', []))}

Output ONLY valid JSON with this exact structure, no markdown:
{{"yes": [...cleaned yes list...], "no": [...cleaned no list...]}}"""

    try:
        from full_auto import llm, get_db
        raw     = llm(prompt, require_json=True)
        cleaned = _json.loads(raw)
        log.debug('[cleanup_cards] ✓ LLM result: yes=%d no=%d',
                  len(cleaned.get('yes', [])), len(cleaned.get('no', [])))

        # If titles mode, sync yes items into profile search_terms
        updated_profile = None
        if mode == "titles" and profile:
            conn = get_db()
            row  = conn.execute("SELECT value FROM profile_cache WHERE key='profile'").fetchone()
            if row:
                p = _json.loads(row["value"])
                before = len(p.get("search_terms", []))
                p["search_terms"] = list(dict.fromkeys(
                    p.get("search_terms", []) + cleaned.get("yes", [])
                ))[:20]
                log.debug('[cleanup_cards] merged titles→search_terms %d→%d', before, len(p["search_terms"]))
                conn.execute("INSERT OR REPLACE INTO profile_cache(key,value) VALUES(?,?)",
                             ("profile", _json.dumps(p)))
                conn.commit()
                updated_profile = p
            conn.close()

        if mode == "skills" and profile:
            conn = get_db()
            row  = conn.execute("SELECT value FROM profile_cache WHERE key='profile'").fetchone()
            if row:
                p = _json.loads(row["value"])
                before = len(p.get("key_skills", []))
                p["key_skills"] = list(dict.fromkeys(
                    p.get("key_skills", []) + cleaned.get("yes", [])
                ))[:20]
                log.debug('[cleanup_cards] merged skills→key_skills %d→%d', before, len(p["key_skills"]))
                conn.execute("INSERT OR REPLACE INTO profile_cache(key,value) VALUES(?,?)",
                             ("profile", _json.dumps(p)))
                conn.commit()
                updated_profile = p
            conn.close()

        return jsonify({"mem": cleaned, "profile": updated_profile})
    except Exception as e:
        log.error('[cleanup_cards] ❌ error: %s', e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/profile", methods=["DELETE"])
def delete_profile():
    """Wipe the entire profile cache and CV file."""
    log.debug('[delete_profile] clearing all profile_cache rows + CV file')
    try:
        from full_auto import get_db, CV_PATH
        conn = get_db()
        row_count = conn.execute("SELECT COUNT(*) FROM profile_cache").fetchone()[0]
        conn.execute("DELETE FROM profile_cache")
        conn.commit()
        conn.close()
        cv_existed = os.path.exists(CV_PATH)
        if cv_existed:
            os.remove(CV_PATH)
        log.debug('[delete_profile] ✓ deleted %d DB rows, CV file deleted=%s', row_count, cv_existed)
        return jsonify({"ok": True})
    except Exception as e:
        log.error('[delete_profile] ❌ error: %s', e)
        return jsonify({"error": str(e)}), 500
@app.route("/api/save_cards", methods=["POST"])
def save_cards():
    mem = request.json
    if not mem:
        log.warning('[save_cards] called with no body')
        return jsonify({"error": "No data"}), 400
    log.debug('[save_cards] received: %s', _mem_summary(mem))
    try:
        save_card_memory(mem)
        log.debug('[save_cards] ✓ persisted to DB')
        return jsonify({"ok": True})
    except Exception as e:
        log.error('[save_cards] ❌ error: %s', e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/load_cards", methods=["GET"])
def load_cards():
    try:
        from full_auto import load_card_memory
        mem = load_card_memory()
        log.debug('[load_cards] loaded from DB: %s', _mem_summary(mem))
        return jsonify({"mem": mem})
    except Exception as e:
        log.error('[load_cards] ❌ error: %s', e)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)