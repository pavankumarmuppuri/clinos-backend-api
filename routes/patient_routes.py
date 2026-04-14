from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

import firebase_config
from firebase_admin import firestore
from auth import get_current_doctor
from utils import route_error, success

firebase_db = firestore.client()
router = APIRouter(prefix="/patients", tags=["Patients"])


# ── Schemas ───────────────────────────────────────────────────────────────────
class BiomarkerIn(BaseModel):
    name: str
    value: str

class PatientCreate(BaseModel):
    mrn: str
    name: str
    age: int
    gender: str
    ecog_status: Optional[str] = None
    cancer_type: str
    stage: Optional[str] = None
    diagnosis_description: Optional[str] = None
    location: Optional[str] = None
    additional_notes: Optional[str] = None
    biomarkers: List[BiomarkerIn] = []

class PatientUpdate(BaseModel):
    mrn: Optional[str] = None
    name: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    ecog_status: Optional[str] = None
    cancer_type: Optional[str] = None
    stage: Optional[str] = None
    diagnosis_description: Optional[str] = None
    location: Optional[str] = None
    additional_notes: Optional[str] = None
    biomarkers: Optional[List[BiomarkerIn]] = None


def _patient_dict(data: dict) -> dict:
    return {
        "id":                    data.get("id"),
        "mrn":                   data.get("mrn"),
        "name":                  data.get("name"),
        "age":                   data.get("age"),
        "gender":                data.get("gender"),
        "ecog_status":           data.get("ecog_status"),
        "cancer_type":           data.get("cancer_type"),
        "stage":                 data.get("stage"),
        "diagnosis_description": data.get("diagnosis_description"),
        "location":              data.get("location"),
        "additional_notes":      data.get("additional_notes"),
        "created_by":            data.get("created_by"),
        "created_at":            data.get("created_at"),
        "biomarkers":            data.get("biomarkers", []),
    }


# ── CREATE ────────────────────────────────────────────────────────────────────
@router.post("", status_code=201)
def create_patient(payload: PatientCreate, current_doctor: dict = Depends(get_current_doctor)):
    patients_ref = firebase_db.collection("patients")

    existing = patients_ref.where("mrn", "==", payload.mrn).get()
    if existing:
        raise route_error(status.HTTP_409_CONFLICT, "mrn_already_exists",
                          f"A patient with MRN '{payload.mrn}' already exists.")

    patient_data = {
        "mrn":                   payload.mrn,
        "name":                  payload.name,
        "age":                   payload.age,
        "gender":                payload.gender,
        "ecog_status":           payload.ecog_status,
        "cancer_type":           payload.cancer_type,
        "stage":                 payload.stage,
        "diagnosis_description": payload.diagnosis_description,
        "location":              payload.location,
        "additional_notes":      payload.additional_notes,
        "created_by":            current_doctor["id"],
        "created_at":            datetime.utcnow().isoformat(),
        "biomarkers":            [{"name": b.name, "value": b.value} for b in payload.biomarkers],
    }

    doc_ref = patients_ref.document()
    patient_data["id"] = doc_ref.id
    doc_ref.set(patient_data)

    return success(data=_patient_dict(patient_data),
                   message=f"Patient '{payload.name}' created successfully.", http_status=201)


# ── LIST ──────────────────────────────────────────────────────────────────────
@router.get("", status_code=200)
def list_patients(current_doctor: dict = Depends(get_current_doctor)):
    patients_ref = firebase_db.collection("patients")
    user_role    = current_doctor.get("role", "doctor")

    if user_role == "patient":
        # Try by created_by first
        docs = list(patients_ref.where("created_by", "==", current_doctor["id"]).stream())
        patients = [_patient_dict(d.to_dict()) for d in docs]

        if not patients:
            # Fallback: match by name (for patients added by a doctor)
            all_docs = patients_ref.stream()
            my_name  = current_doctor.get("name", "").lower()
            patients = [
                _patient_dict(d.to_dict()) for d in all_docs
                if d.to_dict().get("name", "").lower() == my_name
            ]
    else:
        docs     = patients_ref.where("created_by", "==", current_doctor["id"]).stream()
        patients = [_patient_dict(d.to_dict()) for d in docs]

    return success(data=patients, message=f"{len(patients)} patient(s) found.")


# ── GET SINGLE ────────────────────────────────────────────────────────────────
@router.get("/{patient_id}", status_code=200)
def get_patient(patient_id: str, current_doctor: dict = Depends(get_current_doctor)):
    doc = firebase_db.collection("patients").document(patient_id).get()
    if not doc.exists:
        raise route_error(status.HTTP_404_NOT_FOUND, "patient_not_found",
                          f"No patient found with ID {patient_id}.")
    data      = doc.to_dict()
    user_role = current_doctor.get("role", "doctor")

    if user_role == "patient":
        allowed = (
            data.get("created_by") == current_doctor["id"] or
            data.get("name", "").lower() == current_doctor.get("name", "").lower()
        )
        if not allowed:
            raise route_error(status.HTTP_403_FORBIDDEN, "forbidden",
                               "You can only view your own profile.")
    else:
        if data.get("created_by") != current_doctor["id"]:
            raise route_error(status.HTTP_404_NOT_FOUND, "patient_not_found",
                               f"No patient found with ID {patient_id}.")

    return success(data=_patient_dict(data), message="Patient retrieved successfully.")


# ── UPDATE (patient self-edit + doctor edit) ──────────────────────────────────
@router.put("/{patient_id}", status_code=200)
def update_patient(patient_id: str, payload: PatientUpdate,
                   current_doctor: dict = Depends(get_current_doctor)):
    doc_ref = firebase_db.collection("patients").document(patient_id)
    doc     = doc_ref.get()
    if not doc.exists:
        raise route_error(status.HTTP_404_NOT_FOUND, "patient_not_found",
                          f"No patient found with ID {patient_id}.")

    data      = doc.to_dict()
    user_role = current_doctor.get("role", "doctor")

    if user_role == "patient":
        allowed = (
            data.get("created_by") == current_doctor["id"] or
            data.get("name", "").lower() == current_doctor.get("name", "").lower()
        )
        if not allowed:
            raise route_error(status.HTTP_403_FORBIDDEN, "forbidden",
                               "You can only edit your own profile.")
    else:
        if data.get("created_by") != current_doctor["id"]:
            raise route_error(status.HTTP_404_NOT_FOUND, "patient_not_found",
                               f"No patient found with ID {patient_id}.")

    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "biomarkers" in updates:
        updates["biomarkers"] = [{"name": b["name"], "value": b["value"]}
                                  for b in updates["biomarkers"]]
    updates["updated_at"] = datetime.utcnow().isoformat()

    doc_ref.update(updates)
    updated = doc_ref.get().to_dict()
    return success(data=_patient_dict(updated), message="Patient updated successfully.")


# ── DELETE ────────────────────────────────────────────────────────────────────
@router.delete("/{patient_id}", status_code=200)
def delete_patient(patient_id: str, current_doctor: dict = Depends(get_current_doctor)):
    if current_doctor.get("role") == "patient":
        raise route_error(status.HTTP_403_FORBIDDEN, "forbidden",
                          "Patients cannot delete records.")

    doc_ref = firebase_db.collection("patients").document(patient_id)
    doc     = doc_ref.get()
    if not doc.exists or doc.to_dict().get("created_by") != current_doctor["id"]:
        raise route_error(status.HTTP_404_NOT_FOUND, "patient_not_found",
                          f"No patient found with ID {patient_id}.")

    name = doc.to_dict().get("name", patient_id)
    doc_ref.delete()
    return success(data={"id": patient_id}, message=f"Patient '{name}' deleted successfully.")
