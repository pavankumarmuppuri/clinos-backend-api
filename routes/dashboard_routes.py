"""
routes/dashboard_routes.py

Dashboard statistics for:
  - CoordinatorDashboard  →  GET /dashboard/coordinator
  - PatientDashboard      →  GET /dashboard/patient

Returns pre-aggregated data so the React front-end makes ONE request
instead of loading patients + trials + match_results separately and
computing stats client-side.
"""
from fastapi import APIRouter, Depends
from datetime import datetime

import firebase_config
from firebase_admin import firestore
from auth import get_current_doctor
from utils import route_error, success
from fastapi import status as http_status

firebase_db = firestore.client()
router      = APIRouter(prefix="/dashboard", tags=["Coordinators"])


# ── GET /dashboard/coordinator ────────────────────────────────────────────────
@router.get("/coordinator", status_code=200)
def coordinator_dashboard(current_user: dict = Depends(get_current_doctor)):
    """
    Returns everything CoordinatorDashboard needs in one call:
      - 4 metric cards (patients, active trials, eligible matches, avg score)
      - match distribution pie data
      - trial enrollment bar chart data
      - recent matches list (last 4)
      - investigate case count (for action items panel)
    """
    if current_user.get("role") == "patient":
        raise route_error(
            http_status.HTTP_403_FORBIDDEN, "forbidden",
            "Use /dashboard/patient for patient role.",
        )

    uid = current_user["id"]

    # ── Load patients ─────────────────────────────────────────────────────────
    patient_docs = firebase_db.collection("patients")\
        .where("created_by", "==", uid).stream()
    patients = [d.to_dict() for d in patient_docs]

    # ── Load trials ───────────────────────────────────────────────────────────
    trial_docs = firebase_db.collection("trials")\
        .where("created_by", "==", uid).stream()
    trials = [d.to_dict() for d in trial_docs]

    # ── Load match results ────────────────────────────────────────────────────
    match_docs = firebase_db.collection("match_results")\
        .where("created_by", "==", uid).stream()
    matches = [d.to_dict() for d in match_docs]

    # ── Load open investigate cases ───────────────────────────────────────────
    inv_docs = firebase_db.collection("investigate_cases")\
        .where("created_by", "==", uid)\
        .where("status", "==", "open").stream()
    investigate_count = sum(1 for _ in inv_docs)

    # ── Compute metrics ───────────────────────────────────────────────────────
    total_patients  = len(patients)
    active_trials   = sum(1 for t in trials
                          if (t.get("status", "") or "").lower() in ("recruiting", "active"))

    # Match status buckets
    eligible_count       = sum(1 for m in matches if m.get("status") == "eligible")
    likely_eligible      = sum(1 for m in matches if m.get("status") == "likely_eligible")
    potential_count      = sum(1 for m in matches if m.get("status") in ("potential", "potentially_eligible"))
    needs_review_count   = sum(1 for m in matches if m.get("status") == "requires_review")

    scores = [float(m.get("match_score", 0)) for m in matches if m.get("match_score")]
    avg_score = round(sum(scores) / len(scores)) if scores else 0

    # ── Match distribution pie ────────────────────────────────────────────────
    match_distribution = [
        {"name": "Eligible",        "value": eligible_count,     "color": "hsl(142,76%,36%)"},
        {"name": "Likely Eligible", "value": likely_eligible,    "color": "hsl(195,80%,35%)"},
        {"name": "Potential",       "value": potential_count,    "color": "hsl(38,92%,50%)"},
        {"name": "Needs Review",    "value": needs_review_count, "color": "hsl(0,84%,60%)"},
    ]

    # ── Trial enrollment bar chart ────────────────────────────────────────────
    enrollment_data = [
        {
            "name":     (t.get("short_title") or t.get("nct_id") or "Trial")[:20],
            "enrolled": t.get("current_enrollment", 0) or 0,
            "target":   t.get("target_enrollment", 0) or 0,
        }
        for t in trials
        if (t.get("target_enrollment") or 0) > 0
    ][:6]

    # ── Recent matches (last 4, with patient + trial display info) ────────────
    sorted_matches = sorted(
        matches,
        key=lambda m: m.get("created_at", ""),
        reverse=True,
    )[:4]

    patient_map = {p["id"]: p for p in patients}
    trial_map   = {t["id"]: t for t in trials}

    recent_matches = []
    for m in sorted_matches:
        p = patient_map.get(m.get("patient_id"), {})
        t = trial_map.get(m.get("trial_id"), {})
        recent_matches.append({
            "id":           m.get("id"),
            "patient_name": p.get("name", ""),
            "cancer_type":  p.get("cancer_type", ""),
            "trial_name":   t.get("short_title") or t.get("nct_id") or "",
            "match_score":  m.get("match_score", 0),
            "status":       m.get("status", ""),
        })

    # ── Top biomarkers ────────────────────────────────────────────────────────
    bm_count: dict = {}
    for p in patients:
        for bm in (p.get("biomarkers") or []):
            name = bm.get("name", "")
            if name:
                bm_count[name] = bm_count.get(name, 0) + 1
    top_biomarkers = sorted(
        [{"name": k, "count": v} for k, v in bm_count.items()],
        key=lambda x: -x["count"],
    )[:7]

    # ── Greeting ──────────────────────────────────────────────────────────────
    hour     = datetime.utcnow().hour
    greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 17 else "Good evening")

    return success(
        data={
            "greeting":             greeting,
            "subtitle":             f"{total_patients} patient{'s' if total_patients != 1 else ''} · {len(trials)} trial{'s' if len(trials) != 1 else ''}",
            "metrics": {
                "patients_screened":  total_patients,
                "active_trials":      active_trials,
                "eligible_matches":   eligible_count,
                "likely_eligible":    likely_eligible,
                "avg_match_score":    avg_score,
            },
            "match_distribution":   match_distribution,
            "enrollment_data":       enrollment_data,
            "recent_matches":        recent_matches,
            "top_biomarkers":        top_biomarkers,
            "action_items": {
                "investigate_open":  investigate_count,
                "matches_needing_review": needs_review_count,
            },
        },
        message="Coordinator dashboard data retrieved.",
    )


# ── GET /dashboard/patient ────────────────────────────────────────────────────
@router.get("/patient", status_code=200, tags=["Patients"])
def patient_dashboard(current_user: dict = Depends(get_current_doctor)):
    """
    Returns everything PatientDashboard needs in one call:
      - welcome message with name
      - metric cards (matched trials, pending reviews, upcoming appointments, documents)
      - matched trials list with scores
      - upcoming appointments (next 3)
      - health summary (pulled from their patient record)
    """
    uid = current_user["id"]

    # ── Find this user's patient record ──────────────────────────────────────
    # Try created_by first, then name match
    p_docs = firebase_db.collection("patients")\
        .where("created_by", "==", uid).limit(1).stream()
    patients = [d.to_dict() for d in p_docs]

    if not patients:
        name_docs = firebase_db.collection("patients")\
            .where("name", "==", current_user.get("name", "")).limit(1).stream()
        patients = [d.to_dict() for d in name_docs]

    patient = patients[0] if patients else None
    patient_id = patient["id"] if patient else None

    # ── Load match results for this patient ───────────────────────────────────
    matches = []
    if patient_id:
        m_docs = firebase_db.collection("match_results")\
            .where("patient_id", "==", patient_id).stream()
        matches = [d.to_dict() for d in m_docs]

    # ── Load appointments ─────────────────────────────────────────────────────
    appts = []
    if patient_id:
        a_docs = firebase_db.collection("appointments")\
            .where("patient_id", "==", patient_id).stream()
        appts_raw = [d.to_dict() for d in a_docs]
        # Sort by date ascending, upcoming only
        today = datetime.utcnow().date().isoformat()
        appts = sorted(
            [a for a in appts_raw if (a.get("date") or "") >= today],
            key=lambda a: a.get("date", ""),
        )[:3]

    # ── Status counts ─────────────────────────────────────────────────────────
    eligible_count  = sum(1 for m in matches if m.get("status") == "eligible")
    review_count    = sum(1 for m in matches if m.get("status") == "requires_review")
    total_matches   = len(matches)
    upcoming_appts  = len(appts)

    # ── Matched trials with scores ────────────────────────────────────────────
    matched_trials = []
    for m in sorted(matches, key=lambda x: float(x.get("match_score", 0)), reverse=True)[:5]:
        t_doc = firebase_db.collection("trials").document(m.get("trial_id", "")).get()
        trial = t_doc.to_dict() if t_doc.exists else {}
        matched_trials.append({
            "trial_id":    m.get("trial_id"),
            "trial_name":  trial.get("short_title") or trial.get("full_title") or m.get("trial_id", ""),
            "nct_id":      trial.get("nct_id"),
            "match_score": m.get("match_score", 0),
            "status":      m.get("status", ""),
            "notes":       m.get("notes"),
        })

    # ── Health summary from patient record ────────────────────────────────────
    health_summary = {}
    if patient:
        bms = patient.get("biomarkers") or []
        primary_bm = next(
            (b.get("name") + " " + b.get("value", "") for b in bms if b.get("value")),
            None,
        )
        health_summary = {
            "diagnosis":     patient.get("diagnosis_description") or patient.get("cancer_type", ""),
            "ecog_status":   patient.get("ecog_status"),
            "stage":         patient.get("stage"),
            "key_biomarker": primary_bm,
            "prior_treatments": patient.get("additional_notes", ""),
        }

    return success(
        data={
            "welcome":        f"Welcome, {current_user.get('name', 'Patient')}",
            "metrics": {
                "matched_trials":    total_matches,
                "eligible_trials":   eligible_count,
                "pending_reviews":   review_count,
                "upcoming_appts":    upcoming_appts,
                "documents":         0,   # extend when document storage is added
            },
            "matched_trials":  matched_trials,
            "appointments":    appts,
            "health_summary":  health_summary,
            "profile": {
                "name":  current_user.get("name"),
                "email": current_user.get("email"),
                "role":  current_user.get("role"),
            },
        },
        message="Patient dashboard data retrieved.",
    )