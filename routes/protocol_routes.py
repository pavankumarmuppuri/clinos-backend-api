"""
routes/protocol_routes.py

Protocol Management — per-trial eligibility criteria with H1-Gate approval.
Maps to React's ProtocolsPage, ProtocolDetailPage, and CriterionDetailPage.

Each trial can have many criteria documents in Firestore:
  collection: "protocol_criteria"
  Each doc: { id, trial_id, criterion_id, type, natural_language, field,
               operator, threshold, unit, time_window_days,
               requires_human_review, confidence,
               approved, approved_at, reviewed_by, created_by, ... }

Audit trail:
  collection: "audit_log"
  Each doc: { id, action, user_id, user_role, patient_token, trial_id,
               criterion_id, determination, reasoning_note, evidence_hash,
               created_at }
"""
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import hashlib

import firebase_config
from firebase_admin import firestore
from auth import get_current_doctor
from utils import route_error, success

firebase_db = firestore.client()
router      = APIRouter(prefix="/protocols", tags=["Coordinators"])


# ── Schemas ───────────────────────────────────────────────────────────────────
class CriterionCreate(BaseModel):
    trial_id:            str
    criterion_id:        str               # e.g. "IC-01", "EC-03"
    type:                str               # "inclusion" | "exclusion"
    natural_language:    str               # full text of the criterion
    field:               Optional[str] = None   # e.g. "ecog", "creatinine_clearance"
    operator:            Optional[str] = None   # ">=", "<=", "=", "in", "not_in"
    threshold:           Optional[float] = None
    unit:                Optional[str] = None   # e.g. "mL/min", "years"
    time_window_days:    Optional[int] = None
    confidence:          Optional[float] = None  # AI confidence 0-1
    requires_human_review: bool = False
    source_page:         Optional[str] = None


class CriterionApprove(BaseModel):
    reasoning_note:  str
    determination:   str = "approved"    # "approved" | "rejected"
    evidence_hash:   Optional[str] = None


class CriterionUpdate(BaseModel):
    natural_language:    Optional[str] = None
    field:               Optional[str] = None
    operator:            Optional[str] = None
    threshold:           Optional[float] = None
    unit:                Optional[str] = None
    time_window_days:    Optional[int] = None
    confidence:          Optional[float] = None
    requires_human_review: Optional[bool] = None


# ── Serializer ────────────────────────────────────────────────────────────────
def _crit_dict(data: dict) -> dict:
    return {
        "id":                    data.get("id"),
        "trial_id":              data.get("trial_id"),
        "criterion_id":          data.get("criterion_id"),
        "type":                  data.get("type"),
        "natural_language":      data.get("natural_language"),
        "field":                 data.get("field"),
        "operator":              data.get("operator"),
        "threshold":             data.get("threshold"),
        "unit":                  data.get("unit"),
        "time_window_days":      data.get("time_window_days"),
        "confidence":            data.get("confidence"),
        "requires_human_review": data.get("requires_human_review", False),
        "source_page":           data.get("source_page"),
        "approved":              data.get("approved", False),
        "approved_at":           data.get("approved_at"),
        "reviewed_by":           data.get("reviewed_by"),
        "determination":         data.get("determination"),
        "created_by":            data.get("created_by"),
        "created_at":            data.get("created_at"),
        "updated_at":            data.get("updated_at"),
    }


def _audit_dict(data: dict) -> dict:
    return {
        "id":             data.get("id"),
        "action":         data.get("action"),
        "user_id":        data.get("user_id"),
        "user_role":      data.get("user_role"),
        "patient_token":  data.get("patient_token"),
        "trial_id":       data.get("trial_id"),
        "criterion_id":   data.get("criterion_id"),
        "determination":  data.get("determination"),
        "reasoning_note": data.get("reasoning_note"),
        "evidence_hash":  data.get("evidence_hash"),
        "override_value": data.get("override_value"),
        "created_at":     data.get("created_at"),
    }


def _write_audit(
    user_id: str,
    user_role: str,
    action: str,
    trial_id: str = None,
    criterion_id: str = None,
    determination: str = None,
    reasoning_note: str = None,
    patient_token: str = None,
    evidence_hash: str = None,
):
    """Helper: append one row to the audit_log collection."""
    entry = {
        "action":         action,
        "user_id":        user_id,
        "user_role":      user_role,
        "patient_token":  patient_token,
        "trial_id":       trial_id,
        "criterion_id":   criterion_id,
        "determination":  determination,
        "reasoning_note": reasoning_note,
        "evidence_hash":  evidence_hash,
        "created_at":     datetime.utcnow().isoformat(),
    }
    doc_ref     = firebase_db.collection("audit_log").document()
    entry["id"] = doc_ref.id
    doc_ref.set(entry)
    return entry


# ════════════════════════════════════════════════════════════════════════
# PROTOCOL CRITERIA ENDPOINTS
# ════════════════════════════════════════════════════════════════════════

# ── GET /protocols/trials — list trials with protocol summary ─────────────────
@router.get("/trials", status_code=200)
def list_protocols(current_user: dict = Depends(get_current_doctor)):
    """
    Return all trials owned by this user, enriched with criteria counts
    (total / approved / pending).  Powers ProtocolsPage.
    """
    if current_user.get("role") == "patient":
        raise route_error(
            status.HTTP_403_FORBIDDEN, "forbidden",
            "Patients cannot access protocol management.",
        )

    # Load trials
    trial_docs = firebase_db.collection("trials")\
        .where("created_by", "==", current_user["id"]).stream()
    trials = [d.to_dict() for d in trial_docs]

    # Load all criteria for these trials in one pass
    all_criteria = []
    trial_ids = [t["id"] for t in trials if t.get("id")]
    if trial_ids:
        for chunk in [trial_ids[i:i+10] for i in range(0, len(trial_ids), 10)]:
            docs = firebase_db.collection("protocol_criteria")\
                .where("trial_id", "in", chunk).stream()
            all_criteria.extend([d.to_dict() for d in docs])

    # Build summary per trial
    crit_map: dict = {}
    for c in all_criteria:
        tid = c.get("trial_id")
        if tid not in crit_map:
            crit_map[tid] = {"total": 0, "approved": 0, "pending": 0}
        crit_map[tid]["total"] += 1
        if c.get("approved"):
            crit_map[tid]["approved"] += 1
        else:
            crit_map[tid]["pending"] += 1

    result = []
    for t in trials:
        tid   = t.get("id")
        stats = crit_map.get(tid, {"total": 0, "approved": 0, "pending": 0})
        result.append({
            "id":          tid,
            "nct_id":      t.get("nct_id"),
            "short_title": t.get("short_title"),
            "full_title":  t.get("full_title"),
            "phase":       t.get("phase"),
            "status":      t.get("status"),
            "criteria_total":    stats["total"],
            "criteria_approved": stats["approved"],
            "criteria_pending":  stats["pending"],
        })

    return success(data=result, message=f"{len(result)} protocol(s) found.")


# ── GET /protocols/:trial_id/criteria — list all criteria for one trial ───────
@router.get("/{trial_id}/criteria", status_code=200)
def list_criteria(
    trial_id: str,
    current_user: dict = Depends(get_current_doctor),
):
    """Return all criteria for a trial. Powers ProtocolDetailPage."""
    if current_user.get("role") == "patient":
        raise route_error(
            status.HTTP_403_FORBIDDEN, "forbidden",
            "Patients cannot view protocol criteria.",
        )

    # Verify ownership of trial
    doc = firebase_db.collection("trials").document(trial_id).get()
    if not doc.exists or doc.to_dict().get("created_by") != current_user["id"]:
        raise route_error(
            status.HTTP_404_NOT_FOUND, "trial_not_found",
            f"No trial found with ID {trial_id}.",
        )

    crit_docs = firebase_db.collection("protocol_criteria")\
        .where("trial_id", "==", trial_id).stream()
    criteria = [_crit_dict(d.to_dict()) for d in crit_docs]

    # Sort: inclusion first, then exclusion; pending before approved
    criteria.sort(key=lambda c: (
        0 if c["type"] == "inclusion" else 1,
        0 if not c["approved"] else 1,
        c.get("criterion_id", ""),
    ))

    return success(data=criteria, message=f"{len(criteria)} criterion/criteria found.")


# ── POST /protocols/:trial_id/criteria — add a criterion ─────────────────────
@router.post("/{trial_id}/criteria", status_code=201)
def add_criterion(
    trial_id: str,
    payload: CriterionCreate,
    current_user: dict = Depends(get_current_doctor),
):
    """Add one criterion to a trial's protocol."""
    if current_user.get("role") == "patient":
        raise route_error(status.HTTP_403_FORBIDDEN, "forbidden", "Patients cannot add criteria.")

    doc = firebase_db.collection("trials").document(trial_id).get()
    if not doc.exists or doc.to_dict().get("created_by") != current_user["id"]:
        raise route_error(status.HTTP_404_NOT_FOUND, "trial_not_found", f"No trial with ID {trial_id}.")

    crit_data = {
        "trial_id":            trial_id,
        "criterion_id":        payload.criterion_id,
        "type":                payload.type,
        "natural_language":    payload.natural_language,
        "field":               payload.field,
        "operator":            payload.operator,
        "threshold":           payload.threshold,
        "unit":                payload.unit,
        "time_window_days":    payload.time_window_days,
        "confidence":          payload.confidence,
        "requires_human_review": payload.requires_human_review,
        "source_page":         payload.source_page,
        "approved":            False,
        "approved_at":         None,
        "reviewed_by":         None,
        "determination":       None,
        "created_by":          current_user["id"],
        "created_at":          datetime.utcnow().isoformat(),
        "updated_at":          None,
    }

    doc_ref          = firebase_db.collection("protocol_criteria").document()
    crit_data["id"]  = doc_ref.id
    doc_ref.set(crit_data)

    _write_audit(
        user_id=current_user["id"], user_role=current_user.get("role","doctor"),
        action="criterion_created", trial_id=trial_id, criterion_id=payload.criterion_id,
        reasoning_note=f"Added criterion: {payload.natural_language[:80]}",
    )

    return success(data=_crit_dict(crit_data), message="Criterion added.", http_status=201)


# ── PUT /protocols/:trial_id/criteria/:criterion_id — update a criterion ──────
@router.put("/{trial_id}/criteria/{criterion_doc_id}", status_code=200)
def update_criterion(
    trial_id: str,
    criterion_doc_id: str,
    payload: CriterionUpdate,
    current_user: dict = Depends(get_current_doctor),
):
    """Update text or metadata of a criterion (resets approval status)."""
    if current_user.get("role") == "patient":
        raise route_error(status.HTTP_403_FORBIDDEN, "forbidden", "Patients cannot edit criteria.")

    crit_ref = firebase_db.collection("protocol_criteria").document(criterion_doc_id)
    crit_doc = crit_ref.get()

    if not crit_doc.exists or crit_doc.to_dict().get("trial_id") != trial_id:
        raise route_error(status.HTTP_404_NOT_FOUND, "criterion_not_found", "Criterion not found.")

    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
    if not updates:
        raise route_error(status.HTTP_400_BAD_REQUEST, "no_changes", "Nothing to update.")

    # Editing the text resets approval
    if "natural_language" in updates:
        updates["approved"]    = False
        updates["approved_at"] = None
        updates["reviewed_by"] = None

    updates["updated_at"] = datetime.utcnow().isoformat()
    crit_ref.update(updates)

    updated = crit_ref.get().to_dict()
    return success(data=_crit_dict(updated), message="Criterion updated.")


# ── POST /protocols/:trial_id/criteria/:criterion_doc_id/approve — H1 Gate ───
@router.post("/{trial_id}/criteria/{criterion_doc_id}/approve", status_code=200)
def approve_criterion(
    trial_id: str,
    criterion_doc_id: str,
    payload: CriterionApprove,
    current_user: dict = Depends(get_current_doctor),
):
    """
    H1 Gate: approve (or reject) a criterion.
    Only coordinators and PIs can approve.
    Writes an immutable audit log entry.
    """
    role = current_user.get("role", "doctor")
    if role == "patient":
        raise route_error(status.HTTP_403_FORBIDDEN, "forbidden", "Patients cannot approve criteria.")

    crit_ref = firebase_db.collection("protocol_criteria").document(criterion_doc_id)
    crit_doc = crit_ref.get()

    if not crit_doc.exists or crit_doc.to_dict().get("trial_id") != trial_id:
        raise route_error(status.HTTP_404_NOT_FOUND, "criterion_not_found", "Criterion not found.")

    approved = (payload.determination == "approved")
    now      = datetime.utcnow().isoformat()

    # Compute evidence hash for tamper-evidence
    evidence_hash = hashlib.sha256(
        f"{criterion_doc_id}{payload.reasoning_note}{current_user['id']}{now}".encode()
    ).hexdigest()

    crit_ref.update({
        "approved":      approved,
        "approved_at":   now if approved else None,
        "reviewed_by":   current_user.get("name", current_user["id"]),
        "determination": payload.determination,
        "updated_at":    now,
    })

    # Immutable audit log
    audit = _write_audit(
        user_id=current_user["id"],
        user_role=role,
        action="criterion_approved" if approved else "criterion_rejected",
        trial_id=trial_id,
        criterion_id=crit_doc.to_dict().get("criterion_id"),
        determination=payload.determination,
        reasoning_note=payload.reasoning_note,
        evidence_hash=evidence_hash,
    )

    updated = crit_ref.get().to_dict()
    return success(
        data={
            "criterion":   _crit_dict(updated),
            "audit_entry": _audit_dict(audit),
        },
        message=f"Criterion {payload.determination}.",
    )


# ── DELETE /protocols/:trial_id/criteria/:criterion_doc_id ───────────────────
@router.delete("/{trial_id}/criteria/{criterion_doc_id}", status_code=200)
def delete_criterion(
    trial_id: str,
    criterion_doc_id: str,
    current_user: dict = Depends(get_current_doctor),
):
    if current_user.get("role") == "patient":
        raise route_error(status.HTTP_403_FORBIDDEN, "forbidden", "Patients cannot delete criteria.")

    crit_ref = firebase_db.collection("protocol_criteria").document(criterion_doc_id)
    crit_doc = crit_ref.get()

    if not crit_doc.exists or crit_doc.to_dict().get("trial_id") != trial_id:
        raise route_error(status.HTTP_404_NOT_FOUND, "criterion_not_found", "Criterion not found.")

    crit_ref.delete()
    return success(data={"id": criterion_doc_id}, message="Criterion deleted.")


# ════════════════════════════════════════════════════════════════════════
# AUDIT LOG ENDPOINTS
# ════════════════════════════════════════════════════════════════════════

@router.get("/audit", status_code=200)
def list_audit_log(
    trial_id:    Optional[str] = None,
    action:      Optional[str] = None,
    current_user: dict = Depends(get_current_doctor),
):
    """
    Return the audit log for this coordinator.
    Optionally filter by trial_id or action.
    Powers the AuditLogPage.
    """
    if current_user.get("role") == "patient":
        raise route_error(status.HTTP_403_FORBIDDEN, "forbidden", "Patients cannot view audit logs.")

    docs    = firebase_db.collection("audit_log")\
        .where("user_id", "==", current_user["id"])\
        .order_by("created_at", direction=firestore.Query.DESCENDING)\
        .limit(200).stream()
    entries = [_audit_dict(d.to_dict()) for d in docs]

    if trial_id:
        entries = [e for e in entries if e.get("trial_id") == trial_id]
    if action:
        entries = [e for e in entries if e.get("action") == action]

    return success(data=entries, message=f"{len(entries)} audit entry/entries found.")


@router.post("/audit", status_code=201)
def log_audit_event(
    payload: dict,
    current_user: dict = Depends(get_current_doctor),
):
    """
    Write a custom audit event — used by CriterionDetailPage when coordinator
    confirms eligibility or marks a patient ineligible.
    """
    entry = _write_audit(
        user_id=current_user["id"],
        user_role=current_user.get("role", "doctor"),
        action=payload.get("action", "custom_event"),
        trial_id=payload.get("trial_id"),
        criterion_id=payload.get("criterion_id"),
        determination=payload.get("determination"),
        reasoning_note=payload.get("reasoning_note"),
        patient_token=payload.get("patient_token"),
        evidence_hash=hashlib.sha256(
            f"{payload.get('action')}{current_user['id']}{datetime.utcnow().isoformat()}".encode()
        ).hexdigest(),
    )
    return success(data=_audit_dict(entry), message="Audit event logged.", http_status=201)