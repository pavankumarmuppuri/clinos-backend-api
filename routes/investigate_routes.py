"""
routes/investigate_routes.py

Investigate Queue — cases where patient data is missing, ambiguous, or conflicts
with trial eligibility criteria. These block the AI matching from producing a
confident result and require human coordinator action.

Tiers mirror the React InvestigatePage:
  tier_1 — Missing data   (Enter Value / Order Lab)
  tier_2 — Ambiguous data (Request PI Judgment / Escalate)
  tier_3 — Edge case      (Route to Medical Monitor)

Firestore collection: "investigate_cases"
"""
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta, timezone

import firebase_config
from firebase_admin import firestore
from auth import get_current_doctor
from utils import route_error, success

firebase_db = firestore.client()
router      = APIRouter(prefix="/investigate", tags=["Coordinators"])


# ── Schemas ───────────────────────────────────────────────────────────────────
class InvestigateCaseCreate(BaseModel):
    patient_id:     str
    trial_id:       str
    tier:           str          # "tier_1" | "tier_2" | "tier_3"
    reason:         str          # human-readable reason, e.g. "Missing CrCl lab value"
    criterion_text: str          # the trial criterion that is blocked
    last_value:     Optional[str] = None   # last known value, e.g. "58 mL/min (March 1)"
    patient_token:  Optional[str] = None   # anonymised display token, e.g. "PT-A3F8C2"
    trial_name:     Optional[str] = None   # short display name
    sla_hours:      Optional[int] = None   # if set, deadline = now + sla_hours


class InvestigateCaseResolve(BaseModel):
    resolution_note: str
    entered_value:   Optional[str] = None  # for tier_1 "Enter Value"
    action_taken:    Optional[str] = None  # "entered_value"|"ordered_lab"|"pi_judgment"|"escalated"|"medical_monitor"


class InvestigateCaseEscalate(BaseModel):
    escalation_note: str


# ── Serializer ────────────────────────────────────────────────────────────────
def _case_dict(data: dict) -> dict:
    return {
        "id":              data.get("id"),
        "patient_id":      data.get("patient_id"),
        "trial_id":        data.get("trial_id"),
        "tier":            data.get("tier"),
        "reason":          data.get("reason"),
        "criterion_text":  data.get("criterion_text"),
        "last_value":      data.get("last_value"),
        "patient_token":   data.get("patient_token"),
        "trial_name":      data.get("trial_name"),
        "status":          data.get("status", "open"),
        "sla_deadline":    data.get("sla_deadline"),
        "resolution_note": data.get("resolution_note"),
        "entered_value":   data.get("entered_value"),
        "action_taken":    data.get("action_taken"),
        "escalated":       data.get("escalated", False),
        "created_by":      data.get("created_by"),
        "created_at":      data.get("created_at"),
        "resolved_at":     data.get("resolved_at"),
        "updated_at":      data.get("updated_at"),
    }


# ── GET /investigate — list all open cases for this user ─────────────────────
@router.get("", status_code=200)
def list_cases(
    tier:   Optional[str] = None,   # filter by tier_1 / tier_2 / tier_3
    status: Optional[str] = None,   # filter by "open" / "resolved" / "escalated"
    current_user: dict = Depends(get_current_doctor),
):
    """
    Return investigate cases for the current coordinator.
    Patients cannot access investigate cases.
    """
    if current_user.get("role") == "patient":
        raise route_error(
            status.HTTP_403_FORBIDDEN, "forbidden",
            "Patients cannot access investigate cases.",
        )

    ref  = firebase_db.collection("investigate_cases")
    docs = ref.where("created_by", "==", current_user["id"]).stream()
    cases = [_case_dict(d.to_dict()) for d in docs]

    if tier:
        cases = [c for c in cases if c["tier"] == tier]
    if status:
        cases = [c for c in cases if c["status"] == status]

    # Sort: open first, then by sla_deadline ascending
    def sort_key(c):
        order = {"open": 0, "escalated": 1, "resolved": 2}
        sla   = c.get("sla_deadline") or "9999"
        return (order.get(c.get("status", "open"), 3), sla)

    cases.sort(key=sort_key)
    return success(data=cases, message=f"{len(cases)} case(s) found.")


# ── POST /investigate — create a new case ─────────────────────────────────────
@router.post("", status_code=201)
def create_case(
    payload: InvestigateCaseCreate,
    current_user: dict = Depends(get_current_doctor),
):
    """
    Create an investigate case — typically triggered automatically when AI
    matching finds a criterion that cannot be evaluated due to missing/ambiguous data.
    """
    if current_user.get("role") == "patient":
        raise route_error(
            status.HTTP_403_FORBIDDEN, "forbidden",
            "Patients cannot create investigate cases.",
        )

    # Generate patient token if not provided
    import hashlib
    if not payload.patient_token:
        raw = f"{payload.patient_id[:6]}{payload.trial_id[:4]}"
        payload.patient_token = "PT-" + hashlib.md5(raw.encode()).hexdigest()[:6].upper()

    # Calculate SLA deadline
    sla_deadline = None
    if payload.sla_hours:
        sla_deadline = (
            datetime.now(timezone.utc) + timedelta(hours=payload.sla_hours)
        ).isoformat()

    case_data = {
        "patient_id":     payload.patient_id,
        "trial_id":       payload.trial_id,
        "tier":           payload.tier,
        "reason":         payload.reason,
        "criterion_text": payload.criterion_text,
        "last_value":     payload.last_value,
        "patient_token":  payload.patient_token,
        "trial_name":     payload.trial_name,
        "status":         "open",
        "sla_deadline":   sla_deadline,
        "resolution_note": None,
        "entered_value":  None,
        "action_taken":   None,
        "escalated":      False,
        "created_by":     current_user["id"],
        "created_at":     datetime.utcnow().isoformat(),
        "resolved_at":    None,
        "updated_at":     None,
    }

    doc_ref          = firebase_db.collection("investigate_cases").document()
    case_data["id"]  = doc_ref.id
    doc_ref.set(case_data)

    return success(
        data=_case_dict(case_data),
        message="Investigate case created.",
        http_status=201,
    )


# ── GET /investigate/:id ──────────────────────────────────────────────────────
@router.get("/{case_id}", status_code=200)
def get_case(
    case_id: str,
    current_user: dict = Depends(get_current_doctor),
):
    doc = firebase_db.collection("investigate_cases").document(case_id).get()
    if not doc.exists or doc.to_dict().get("created_by") != current_user["id"]:
        raise route_error(
            status.HTTP_404_NOT_FOUND, "case_not_found",
            f"No investigate case found with ID {case_id}.",
        )
    return success(data=_case_dict(doc.to_dict()), message="Case retrieved.")


# ── POST /investigate/:id/resolve ─────────────────────────────────────────────
@router.post("/{case_id}/resolve", status_code=200)
def resolve_case(
    case_id: str,
    payload: InvestigateCaseResolve,
    current_user: dict = Depends(get_current_doctor),
):
    """
    Resolve a case after coordinator action.
    - tier_1: Enter Value or Order Lab
    - tier_2: PI Judgment received
    - tier_3: Medical Monitor routing complete
    """
    doc_ref = firebase_db.collection("investigate_cases").document(case_id)
    doc     = doc_ref.get()

    if not doc.exists or doc.to_dict().get("created_by") != current_user["id"]:
        raise route_error(
            status.HTTP_404_NOT_FOUND, "case_not_found",
            f"No investigate case found with ID {case_id}.",
        )

    now = datetime.utcnow().isoformat()
    doc_ref.update({
        "status":          "resolved",
        "resolution_note": payload.resolution_note,
        "entered_value":   payload.entered_value,
        "action_taken":    payload.action_taken,
        "resolved_at":     now,
        "updated_at":      now,
    })

    updated = doc_ref.get().to_dict()
    return success(data=_case_dict(updated), message="Case resolved successfully.")


# ── POST /investigate/:id/escalate ────────────────────────────────────────────
@router.post("/{case_id}/escalate", status_code=200)
def escalate_case(
    case_id: str,
    payload: InvestigateCaseEscalate,
    current_user: dict = Depends(get_current_doctor),
):
    """Escalate a tier_2 case — flags it for PI or medical monitor attention."""
    doc_ref = firebase_db.collection("investigate_cases").document(case_id)
    doc     = doc_ref.get()

    if not doc.exists or doc.to_dict().get("created_by") != current_user["id"]:
        raise route_error(
            status.HTTP_404_NOT_FOUND, "case_not_found",
            f"No investigate case found with ID {case_id}.",
        )

    now = datetime.utcnow().isoformat()
    doc_ref.update({
        "status":          "escalated",
        "escalated":       True,
        "resolution_note": payload.escalation_note,
        "updated_at":      now,
    })

    updated = doc_ref.get().to_dict()
    return success(data=_case_dict(updated), message="Case escalated.")


# ── DELETE /investigate/:id ───────────────────────────────────────────────────
@router.delete("/{case_id}", status_code=200)
def delete_case(
    case_id: str,
    current_user: dict = Depends(get_current_doctor),
):
    """Remove a case (e.g. created in error)."""
    if current_user.get("role") == "patient":
        raise route_error(
            status.HTTP_403_FORBIDDEN, "forbidden",
            "Patients cannot delete investigate cases.",
        )

    doc_ref = firebase_db.collection("investigate_cases").document(case_id)
    doc     = doc_ref.get()

    if not doc.exists or doc.to_dict().get("created_by") != current_user["id"]:
        raise route_error(
            status.HTTP_404_NOT_FOUND, "case_not_found",
            f"No investigate case found with ID {case_id}.",
        )

    doc_ref.delete()
    return success(data={"id": case_id}, message="Case deleted.")


# ── GET /investigate/stats/summary ───────────────────────────────────────────
@router.get("/stats/summary", status_code=200)
def case_stats(current_user: dict = Depends(get_current_doctor)):
    """
    Return counts per tier and status — used by the dashboard action items panel
    and the NotificationBell to show the open investigate count.
    """
    if current_user.get("role") == "patient":
        return success(data={"total": 0, "open": 0, "tier_1": 0, "tier_2": 0, "tier_3": 0})

    docs  = firebase_db.collection("investigate_cases")\
        .where("created_by", "==", current_user["id"])\
        .where("status", "==", "open")\
        .stream()
    cases = [d.to_dict() for d in docs]

    return success(data={
        "total":  len(cases),
        "open":   len(cases),
        "tier_1": sum(1 for c in cases if c.get("tier") == "tier_1"),
        "tier_2": sum(1 for c in cases if c.get("tier") == "tier_2"),
        "tier_3": sum(1 for c in cases if c.get("tier") == "tier_3"),
    }, message="Investigate stats retrieved.")