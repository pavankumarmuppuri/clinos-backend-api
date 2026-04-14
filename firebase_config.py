import firebase_admin
from firebase_admin import credentials
import os
import json

if not firebase_admin._apps:
    # Railway / production — credentials stored as env var
    cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if cred_json:
        cred = credentials.Certificate(json.loads(cred_json))
    else:
        # Local dev — read from file
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        cred_paths = [
            os.path.join(BASE_DIR, "firebase_credentials.json"),
            os.path.join(BASE_DIR, "firebase", "firebase_credentials.json"),
            os.getenv("FIREBASE_CRED_PATH", ""),
        ]
        cred_path = next((p for p in cred_paths if p and os.path.exists(p)), None)
        if not cred_path:
            raise RuntimeError(
                "Firebase credentials not found. "
                "Set FIREBASE_CREDENTIALS_JSON env var on Railway, "
                "or place firebase_credentials.json in the project root locally."
            )
        cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
