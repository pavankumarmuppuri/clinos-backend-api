"""
routes/appointment_routes.py
Full CRUD for patient appointments.

Firestore collection: "appointments"
Each document:
  id, patient_id, title, appointment_type, trial_id, trial_name,
  date, time, location, provider, status, notes,
  created_by, created_at, updated_at
"""
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

import firebase_config
from firebase_admin import firestore
from auth import get_current_doctor
from utils import route_error, success

firebase_db = firestore.client()
router      = APIRouter(prefix="/appointments", tags=["Patients"])


# ── Schemas ───────────────────────────────────────────────────────────────────
class AppointmentCreate(BaseModel):
    patient_id:       str
    title:            str                    # e.g. "Screening Visit"
    appointment_type: str = "visit"          # "visit" | "lab" | "consult" | "procedure"
    trial_id:         Optional[str] = None   # Firebase trial doc ID (if linked to a trial)
    trial_name:       Optional[str] = None   # e.g. "FLAURA2"
    date:             str                    # ISO date string  e.g. "2026-04-14"
    time:             Optional[str] = None   # e.g. "10:00 AM"
    location:         Optional[str] = None
    provider:         Optional[str] = None
    status:           str = "scheduled"      # "scheduled" | "confirmed" | "completed" | "cancelled"
    notes:            Optional[str] = None


class AppointmentUpdate(BaseModel):
    title:            Optional[str] = None
    appointment_type: Optional[str] = None
    trial_id:         Optional[str] = None
    trial_name:       Optional[str] = None
    date:             Optional[str] = None
    time:             Optional[str] = None
    location:         Optional[str] = None
    provider:         Optional[str] = None
    status:           Optional[str] = None
    notes:            Optional[str] = None


# ── Serializer ────────────────────────────────────────────────────────────────
def _appt_dict(data: dict) -> dict:
    return {
        "id":               data.get("id"),
        "patient_id":       data.get("patient_id"),
        "title":            data.get("title"),
        "appointment_type": data.get("appointment_type", "visit"),
        "trial_id":         data.get("trial_id"),
        "trial_name":       data.get("trial_name"),
        "date":             data.get("date"),
        "time":             data.get("time"),
        "location":         data.get("location"),
        "provider":         data.get("provider"),
        "status":           data.get("status", "scheduled"),
        "notes":            data.get("notes"),
        "created_by":       data.get("created_by"),
        "created_at":       data.get("created_at"),
        "updated_at":       data.get("updated_at"),
    }


# ── Access helpers ────────────────────────────────────────────────────────────
def _can_access_appointment(appt_data: dict, current_user: dict) -> bool:
    """
    Coordinators can see appointments they created.
    Patients can only see their own appointments (matched by patient_id or created_by).
    """
    role = current_user.get("role", "doctor")
    if role == "patient":
        # Patient sees appointments linked to their patient record
        patient_doc = _get_patient_for_user(current_user)
        if patient_doc and appt_data.get("patient_id") == patient_doc:
            return True
        # Fallback: appointments they created themselves
        return appt_data.get("created_by") == current_user["id"]
    else:
        # Coordinator sees what they created
        return appt_data.get("created_by") == current_user["id"]


def _get_patient_for_user(current_user: dict) -> Optional[str]:
    """
    Find the Firestore patient document ID for the logged-in patient user.
    Matches on created_by first, then name.
    """
    try:
        docs = firebase_db.collection("patients")\
            .where("created_by", "==", current_user["id"])\
            .limit(1).get()
        if docs:
            return docs[0].id
        # Fallback: name match
        name_docs = firebase_db.collection("patients")\
            .where("name", "==", current_user.get("name", ""))\
            .limit(1).get()
        if name_docs:
            return name_docs[0].id
    except Exception:
        pass
    return None


# ── CREATE  POST /appointments ────────────────────────────────────────────────
@router.post("", status_code=201)
def create_appointment(
    payload: AppointmentCreate,
    current_user: dict = Depends(get_current_doctor),
):
    """
    Book an appointment.
    - Coordinators can create for any of their patients.
    - Patients can only create for themselves.
    """
    role = current_user.get("role", "doctor")

    # Patients may only book for themselves
    if role == "patient":
        patient_id = _get_patient_for_user(current_user)
        if not patient_id:
            raise route_error(
                status.HTTP_404_NOT_FOUND, "patient_not_found",
                "Your patient profile was not found. Please set up your profile first.",
            )
        if payload.patient_id and payload.patient_id != patient_id:
            raise route_error(
                status.HTTP_403_FORBIDDEN, "forbidden",
                "Patients can only create appointments for themselves.",
            )
        effective_patient_id = patient_id
    else:
        # Verify the coordinator owns the patient
        doc = firebase_db.collection("patients").document(payload.patient_id).get()
        if not doc.exists or doc.to_dict().get("created_by") != current_user["id"]:
            raise route_error(
                status.HTTP_404_NOT_FOUND, "patient_not_found",
                f"No patient found with ID {payload.patient_id}.",
            )
        effective_patient_id = payload.patient_id

    appt_data = {
        "patient_id":       effective_patient_id,
        "title":            payload.title,
        "appointment_type": payload.appointment_type,
        "trial_id":         payload.trial_id,
        "trial_name":       payload.trial_name,
        "date":             payload.date,
        "time":             payload.time,
        "location":         payload.location,
        "provider":         payload.provider,
        "status":           payload.status,
        "notes":            payload.notes,
        "created_by":       current_user["id"],
        "created_at":       datetime.utcnow().isoformat(),
        "updated_at":       None,
    }

    doc_ref         = firebase_db.collection("appointments").document()
    appt_data["id"] = doc_ref.id
    doc_ref.set(appt_data)

    return success(
        data=_appt_dict(appt_data),
        message=f"Appointment '{payload.title}' created successfully.",
        http_status=201,
    )


# ── LIST  GET /appointments ───────────────────────────────────────────────────
@router.get("", status_code=200)
def list_appointments(
    patient_id: Optional[str] = None,
    current_user: dict = Depends(get_current_doctor),
):
    """
    List appointments.
    - Coordinators: all appointments they created, optionally filtered by patient_id.
    - Patients: only their own appointments.
    """
    role = current_user.get("role", "doctor")
    ref  = firebase_db.collection("appointments")

    if role == "patient":
        my_patient_id = _get_patient_for_user(current_user)
        if my_patient_id:
            docs = ref.where("patient_id", "==", my_patient_id).stream()
        else:
            docs = ref.where("created_by", "==", current_user["id"]).stream()
    else:
        if patient_id:
            docs = ref.where("created_by", "==", current_user["id"])\
                      .where("patient_id", "==", patient_id).stream()
        else:
            docs = ref.where("created_by", "==", current_user["id"]).stream()

    appts = [_appt_dict(d.to_dict()) for d in docs]
    # Sort by date ascending
    appts.sort(key=lambda a: a.get("date") or "")

    return success(data=appts, message=f"{len(appts)} appointment(s) found.")


# ── GET SINGLE  GET /appointments/:id ─────────────────────────────────────────
@router.get("/{appointment_id}", status_code=200)
def get_appointment(
    appointment_id: str,
    current_user: dict = Depends(get_current_doctor),
):
    doc = firebase_db.collection("appointments").document(appointment_id).get()
    if not doc.exists:
        raise route_error(
            status.HTTP_404_NOT_FOUND, "appointment_not_found",
            f"No appointment found with ID {appointment_id}.",
        )
    data = doc.to_dict()
    if not _can_access_appointment(data, current_user):
        raise route_error(
            status.HTTP_403_FORBIDDEN, "forbidden",
            "You do not have permission to view this appointment.",
        )
    return success(data=_appt_dict(data), message="Appointment retrieved successfully.")


# ── UPDATE  PUT /appointments/:id ─────────────────────────────────────────────
@router.put("/{appointment_id}", status_code=200)
def update_appointment(
    appointment_id: str,
    payload: AppointmentUpdate,
    current_user: dict = Depends(get_current_doctor),
):
    """
    Update an appointment.  Can be used to confirm, reschedule, or add notes.
    Patients can update status/notes on their own appointments.
    Coordinators can update any field on appointments they created.
    """
    doc_ref = firebase_db.collection("appointments").document(appointment_id)
    doc     = doc_ref.get()

    if not doc.exists:
        raise route_error(
            status.HTTP_404_NOT_FOUND, "appointment_not_found",
            f"No appointment found with ID {appointment_id}.",
        )

    data = doc.to_dict()
    if not _can_access_appointment(data, current_user):
        raise route_error(
            status.HTTP_403_FORBIDDEN, "forbidden",
            "You do not have permission to update this appointment.",
        )

    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}

    # Patients may only update status and notes — not date/time/location etc.
    if current_user.get("role") == "patient":
        allowed_patient_fields = {"status", "notes"}
        updates = {k: v for k, v in updates.items() if k in allowed_patient_fields}

    if not updates:
        raise route_error(
            status.HTTP_400_BAD_REQUEST, "no_changes",
            "No fields to update were provided.",
        )

    updates["updated_at"] = datetime.utcnow().isoformat()
    doc_ref.update(updates)

    updated = doc_ref.get().to_dict()
    return success(
        data=_appt_dict(updated),
        message="Appointment updated successfully.",
    )


# ── DELETE  DELETE /appointments/:id ──────────────────────────────────────────
@router.delete("/{appointment_id}", status_code=200)
def delete_appointment(
    appointment_id: str,
    current_user: dict = Depends(get_current_doctor),
):
    """
    Cancel / remove an appointment.
    Patients can only cancel their own; coordinators can delete theirs.
    """
    doc_ref = firebase_db.collection("appointments").document(appointment_id)
    doc     = doc_ref.get()

    if not doc.exists:
        raise route_error(
            status.HTTP_404_NOT_FOUND, "appointment_not_found",
            f"No appointment found with ID {appointment_id}.",
        )

    data = doc.to_dict()
    if not _can_access_appointment(data, current_user):
        raise route_error(
            status.HTTP_403_FORBIDDEN, "forbidden",
            "You do not have permission to delete this appointment.",
        )

    title = data.get("title", appointment_id)
    doc_ref.delete()
    return success(
        data={"id": appointment_id},
        message=f"Appointment '{title}' deleted successfully.",
    )


# ── CONFIRM  POST /appointments/:id/confirm ───────────────────────────────────
@router.post("/{appointment_id}/confirm", status_code=200)
def confirm_appointment(
    appointment_id: str,
    current_user: dict = Depends(get_current_doctor),
):
    """Shortcut to mark an appointment as confirmed."""
    doc_ref = firebase_db.collection("appointments").document(appointment_id)
    doc     = doc_ref.get()

    if not doc.exists:
        raise route_error(
            status.HTTP_404_NOT_FOUND, "appointment_not_found",
            f"No appointment found with ID {appointment_id}.",
        )

    if not _can_access_appointment(doc.to_dict(), current_user):
        raise route_error(
            status.HTTP_403_FORBIDDEN, "forbidden",
            "You do not have permission to confirm this appointment.",
        )

    doc_ref.update({
        "status":     "confirmed",
        "updated_at": datetime.utcnow().isoformat(),
    })
    updated = doc_ref.get().to_dict()
    return success(data=_appt_dict(updated), message="Appointment confirmed.")


# ── CANCEL  POST /appointments/:id/cancel ─────────────────────────────────────
@router.post("/{appointment_id}/cancel", status_code=200)
def cancel_appointment(
    appointment_id: str,
    current_user: dict = Depends(get_current_doctor),
):
    """Shortcut to mark an appointment as cancelled."""
    doc_ref = firebase_db.collection("appointments").document(appointment_id)
    doc     = doc_ref.get()

    if not doc.exists:
        raise route_error(
            status.HTTP_404_NOT_FOUND, "appointment_not_found",
            f"No appointment found with ID {appointment_id}.",
        )

    if not _can_access_appointment(doc.to_dict(), current_user):
        raise route_error(
            status.HTTP_403_FORBIDDEN, "forbidden",
            "You do not have permission to cancel this appointment.",
        )

    doc_ref.update({
        "status":     "cancelled",
        "updated_at": datetime.utcnow().isoformat(),
    })
    updated = doc_ref.get().to_dict()
    return success(data=_appt_dict(updated), message="Appointment cancelled.")