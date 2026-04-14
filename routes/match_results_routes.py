"""
routes/match_results_routes.py

Persistent match results storage — stores the output of /api/match/score
so the React MatchResultsPage, PatientTrialsPage, and PatientDashboard
can retrieve results without re-running the AI pipeline.

Firestore collection: "match_results"
Each doc mirrors the MatchResult interface in useMatchResults.ts:
  { id, patient_id, trial_id, match_score, status, criteria_results,
    notes, reviewed_at, reviewed_by, created_by, created_at, updated_at }
"""
from fastapi import APIRouter, Depends, status as http_status
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

import firebase_config
from firebase_admin import firestore
from auth import get_current_doctor
from utils import route_error, success

firebase_db = firestore.client()
router      = APIRouter(prefix="/api/match/results", tags=["Coordinators"])


# ── Schemas ───────────────────────────────────────────────────────────────────
class CriterionResult(BaseModel):
    criterionId:          str = ""
    criterionDescription: str = ""
    type:                 str = "inclusion"
    result:               str = "unknown"   # met|not_met|unknown|requires_verification
    explanation:          str = ""
    patientValue:         Optional[str] = None
    requiredValue:        Optional[str] = None


class MatchResultCreate(BaseModel):
    patient_id:       str
    trial_id:         str
    match_score:      float
    status:           str = "requires_review"
    criteria_results: List[CriterionResult] = []
    notes:            Optional[str] = None


class MatchResultUpdate(BaseModel):
    status:           Optional[str] = None
    criteria_results: Optional[List[CriterionResult]] = None
    notes:            Optional[str] = None
    reviewed_by:      Optional[str] = None


# ── Serializer ────────────────────────────────────────────────────────────────
def _result_dict(data: dict) -> dict:
    return {
        "id":              data.get("id"),
        "user_id":         data.get("created_by"),
        "patient_id":      data.get("patient_id"),
        "trial_id":        data.get("trial_id"),
        "match_score":     data.get("match_score", 0),
        "status":          data.get("status", "requires_review"),
        "criteria_results": data.get("criteria_results", []),
        "notes":           data.get("notes"),
        "reviewed_at":     data.get("reviewed_at"),
        "reviewed_by":     data.get("reviewed_by"),
        "created_at":      data.get("created_at"),
        "updated_at":      data.get("updated_at"),
    }


# ── GET /api/match/results ────────────────────────────────────────────────────
@router.get("", status_code=200)
def list_results(
    patient_id: Optional[str] = None,
    trial_id:   Optional[str] = None,
    status:     Optional[str] = None,
    current_user: dict = Depends(get_current_doctor),
):
    """
    Return saved match results.
    - Coordinators see results they created.
    - Patients see results linked to their patient record.
    """
    uid  = current_user["id"]
    role = current_user.get("role", "doctor")
    ref  = firebase_db.collection("match_results")

    if role == "patient":
        # Find patient record first
        p_docs = ref.where("created_by", "==", uid).limit(1).stream()
        pid_docs = list(firebase_db.collection("patients")
                        .where("created_by", "==", uid).limit(1).stream())
        if pid_docs:
            docs = ref.where("patient_id", "==", pid_docs[0].id).stream()
        else:
            docs = ref.where("created_by", "==", uid).stream()
    else:
        docs = ref.where("created_by", "==", uid).stream()

    results = [_result_dict(d.to_dict()) for d in docs]

    if patient_id:
        results = [r for r in results if r["patient_id"] == patient_id]
    if trial_id:
        results = [r for r in results if r["trial_id"] == trial_id]
    if status:
        results = [r for r in results if r["status"] == status]

    results.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return success(data=results, message=f"{len(results)} result(s) found.")


# ── POST /api/match/results ───────────────────────────────────────────────────
@router.post("", status_code=201)
def save_result(
    payload: MatchResultCreate,
    current_user: dict = Depends(get_current_doctor),
):
    """
    Persist one match result.  Called by the React front-end after
    /api/match/:patient_id returns scored candidates, so results survive
    page reloads without re-running the AI pipeline.
    """
    data = {
        "patient_id":      payload.patient_id,
        "trial_id":        payload.trial_id,
        "match_score":     payload.match_score,
        "status":          payload.status,
        "criteria_results": [cr.model_dump() for cr in payload.criteria_results],
        "notes":           payload.notes,
        "reviewed_at":     None,
        "reviewed_by":     None,
        "created_by":      current_user["id"],
        "created_at":      datetime.utcnow().isoformat(),
        "updated_at":      None,
    }

    doc_ref      = firebase_db.collection("match_results").document()
    data["id"]   = doc_ref.id
    doc_ref.set(data)

    return success(data=_result_dict(data), message="Match result saved.", http_status=201)


# ── GET /api/match/results/:id ────────────────────────────────────────────────
@router.get("/{result_id}", status_code=200)
def get_result(
    result_id: str,
    current_user: dict = Depends(get_current_doctor),
):
    doc = firebase_db.collection("match_results").document(result_id).get()
    if not doc.exists:
        raise route_error(http_status.HTTP_404_NOT_FOUND, "not_found", f"No result with ID {result_id}.")

    data = doc.to_dict()
    role = current_user.get("role", "doctor")

    # Access check
    if role != "patient" and data.get("created_by") != current_user["id"]:
        raise route_error(http_status.HTTP_403_FORBIDDEN, "forbidden", "Access denied.")

    return success(data=_result_dict(data), message="Result retrieved.")


# ── PUT /api/match/results/:id ────────────────────────────────────────────────
@router.put("/{result_id}", status_code=200)
def update_result(
    result_id: str,
    payload: MatchResultUpdate,
    current_user: dict = Depends(get_current_doctor),
):
    """
    Update status or add coordinator review.
    Used by CriterionDetailPage when coordinator confirms eligibility.
    """
    doc_ref = firebase_db.collection("match_results").document(result_id)
    doc     = doc_ref.get()

    if not doc.exists:
        raise route_error(http_status.HTTP_404_NOT_FOUND, "not_found", f"No result with ID {result_id}.")
    if doc.to_dict().get("created_by") != current_user["id"]:
        raise route_error(http_status.HTTP_403_FORBIDDEN, "forbidden", "Access denied.")

    updates: dict = {}
    if payload.status           is not None: updates["status"]           = payload.status
    if payload.notes            is not None: updates["notes"]            = payload.notes
    if payload.criteria_results is not None:
        updates["criteria_results"] = [cr.model_dump() for cr in payload.criteria_results]
    if payload.reviewed_by      is not None:
        updates["reviewed_by"] = payload.reviewed_by
        updates["reviewed_at"] = datetime.utcnow().isoformat()

    if not updates:
        raise route_error(http_status.HTTP_400_BAD_REQUEST, "no_changes", "Nothing to update.")

    updates["updated_at"] = datetime.utcnow().isoformat()
    doc_ref.update(updates)

    updated = doc_ref.get().to_dict()
    return success(data=_result_dict(updated), message="Result updated.")


# ── DELETE /api/match/results/:id ─────────────────────────────────────────────
@router.delete("/{result_id}", status_code=200)
def delete_result(
    result_id: str,
    current_user: dict = Depends(get_current_doctor),
):
    doc_ref = firebase_db.collection("match_results").document(result_id)
    doc     = doc_ref.get()

    if not doc.exists:
        raise route_error(http_status.HTTP_404_NOT_FOUND, "not_found", f"No result with ID {result_id}.")
    if doc.to_dict().get("created_by") != current_user["id"]:
        raise route_error(http_status.HTTP_403_FORBIDDEN, "forbidden", "Access denied.")

    doc_ref.delete()
    return success(data={"id": result_id}, message="Result deleted.")