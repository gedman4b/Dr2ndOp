from datetime import datetime
from dateutil import parser, relativedelta
import json
import os, time, re, sys
import uuid
import requests
import jwt
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
from fhirpy import SyncFHIRClient

load_dotenv()

EPIC_PRIVATE_KEY_PATH=os.getenv("EPIC_PRIVATE_KEY_PATH")
EPIC_CLIENT_ID=os.getenv("EPIC_CLIENT_ID")
EPIC_PRIVATE_KEY_PATH=os.getenv("EPIC_PRIVATE_KEY_PATH")
EPIC_TOKEN_URL=os.getenv("EPIC_TOKEN_URL")
EPIC_FHIR_BASE=os.getenv("EPIC_FHIR_BASE")
EPIC_FHIR_MED_BASE=os.getenv("EPIC_FHIR_MED_BASE")
EPIC_TEST_PATIENT_ID=os.getenv("EPIC_TEST_PATIENT_ID")
EPIC_JWK_KID=os.getenv("EPIC_JWK_KID")

class EpicFHIRService:
    """
    Encapsulates Epic Backend Services JWT, token exchange, and convenience
    methods to fetch a patient snapshot (patient summary, observations, allergies,
    conditions, medications) using fhirpy.

    You can instantiate with explicit args or let it read from environment.
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        token_url: Optional[str] = None,
        fhir_base: Optional[str] = None,
        fhir_med_base: Optional[str] = None,
        private_key_path: Optional[str] = None,
        jwk_kid: Optional[str] = None,
        jwt_alg: str = "RS384",
    ) -> None:
        self.client_id = client_id or EPIC_CLIENT_ID
        self.token_url = token_url or EPIC_TOKEN_URL
        self.fhir_base = fhir_base or EPIC_FHIR_BASE
        self.fhir_med_base = fhir_med_base or EPIC_FHIR_MED_BASE or self.fhir_base
        self.private_key_path = private_key_path or EPIC_PRIVATE_KEY_PATH
        self.jwk_kid = jwk_kid or EPIC_JWK_KID
        self.jwt_alg = jwt_alg

# ---------- Public API ----------

    def get_access_token(self) -> Dict[str, Any]:
        """
        Exchange a signed JWT for an access token using Epic's Backend Services flow.
        Returns the parsed JSON response (includes access_token, expires_in, etc.).
        """
        assertion = self._build_backend_jwt()
        data = {
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
        }
        resp = requests.post(self.token_url, data=data, timeout=30)
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"Token request failed ({resp.status_code}): {resp.text}") from e
        try:
            return resp.json()
        except requests.exceptions.JSONDecodeError as jde:
            raise RuntimeError("Token response was not valid JSON") from jde

    def build_fhir_client(self, base_url: str, access_token: str) -> SyncFHIRClient:
        """
        Build a fhirpy SyncFHIRClient with the Bearer token attached.
        """
        return SyncFHIRClient(base_url, authorization=f"Bearer {access_token}")

    def snapshot(self, patient_id: str) -> Dict[str, Any]:
        """
        Fetch a full patient snapshot:
          - patient (id, name, dob, gender, GP)
          - observations (laboratory)
          - allergies
          - conditions (medical-history)
          - medications (via med base if provided)
        Returns a JSON-serializable dict mirroring your original script.
        """
        limit = os.getenv("EPIC_SET_LIMIT", "0")

        # 1) Get token
        token_resp = self.get_access_token()
        access_token = token_resp["access_token"]

        # 2) FHIR clients
        fhir = self.build_fhir_client(self.fhir_base, access_token)
        fhir_med = self.build_fhir_client(self.fhir_med_base, access_token)

        # 3) Patient
        patient = self._patient_summary(fhir, patient_id)

        # 4) Observations (lab)
        now = datetime.now()
        yearago = relativedelta.relativedelta(years=1)  # Labs older than 1 year ago are being dropped
        if limit == "1":
            obs = fhir.resources("Observation").search(
              patient=patient_id, category="laboratory", date=f"ge{(now - yearago).isoformat()}"
            ).limit(10).fetch()
        else:
            obs = fhir.resources("Observation").search(
              patient=patient_id, category="laboratory", date=f"ge{(now - yearago).isoformat()}"
            ).fetch_all()
        obs_list = [self._summarize_observation(o) for o in (obs or [])]
        
        # De-duplicate observations
        seen_obs_texts = set()
        observations = []
        for obs in obs_list:
            if obs['text'] not in seen_obs_texts:
                if obs['issued'] is not None:  # remove labs with no issue date
                    observations.append(obs)
                    seen_obs_texts.add(obs['text'])
        
        # 5) Allergies
        years_past = relativedelta.relativedelta(years=3)
        if limit == "1":
            allergies_raw = fhir.resources("AllergyIntolerance").search(
             patient=patient_id, clinicalStatus="active", lastUpdated=f"ge{(now - years_past).isoformat()}"
            ).limit(10).fetch()
        else:
            allergies_raw = fhir.resources("AllergyIntolerance").search(
            patient=patient_id, clinicalStatus="active", lastUpdated=f"ge{(now - years_past).isoformat()}"
        ).fetch_all()
        allergies_list = [self._summarize_allergy(a) for a in (allergies_raw or [])]

        # De-duplicate allergies
        seen_allergy_texts = set()
        allergies = []
        for allergy in allergies_list:
            if allergy['text'] not in seen_allergy_texts:
                allergies.append(allergy)
                seen_allergy_texts.add(allergy['text'])

        # 6) Conditions
        if limit == "1":
            conditions_raw = fhir.resources("Condition").search(
             patient=patient_id, category="medical-history").limit(10).fetch()
        else:
             conditions_raw = fhir.resources("Condition").search(
             patient=patient_id, category="medical-history").fetch_all()
        conditions_list = [self._summarize_condition(c) for c in (conditions_raw or [])]

        # De-duplicate conditions
        seen_condition_texts = set()
        conditions = []
        for cond in conditions_list:
            if cond['text'] not in seen_condition_texts:
                conditions.append(cond)
                seen_condition_texts.add(cond['text'])
        
        # 7) Medications (Epic often splits meds to a different base)
        if limit == "1":
            meds_raw = fhir_med.resources("MedicationStatement").search(
             patient=patient_id
            ).limit(20).fetch()
        else:
            meds_raw = fhir_med.resources("MedicationStatement").search(
             patient=patient_id
            ).fetch_all()
        meds_req_list = [self._summarize_medication(m) for m in (meds_raw or [])]

        # De-duplicate medications
        seen_meds = set()
        medications = []
        for med in meds_req_list:
            # Use a tuple of name and dosage to identify duplicates
            med_tuple = (med.get('name'), med.get('dosage'))
            if med_tuple not in seen_meds:
                medications.append(med)
                seen_meds.add(med_tuple)
        
        return {
            "patient": patient,
            "observations": observations,
            "allergies": allergies,
            "conditions": conditions,
            "medications": medications,
        }

# ---------- Private helpers ----------

    def _load_private_key(self) -> str:
        if not self.private_key_path:
            raise RuntimeError("Set EPIC_PRIVATE_KEY_PATH or pass private_key_path=…")
        with open(self.private_key_path, "r", encoding="utf-8") as f:
            return f.read()

    def _build_backend_jwt(self) -> str:
        """
        Build a one-time JWT for Epic Backend Services.
        Uses RS384 by default (same as your original).
        """
        now = int(time.time())
        claims = {
            "iss": self.client_id,
            "sub": self.client_id,
            "aud": self.token_url,
            "jti": str(uuid.uuid4()),
            "exp": now + 180,  # <= 5 minutes from iat
            "nbf": now,
            "iat": now,
        }
        headers = {"alg": self.jwt_alg, "typ": "JWT"}
        if self.jwk_kid:
            headers["kid"] = self.jwk_kid
        private_key = self._load_private_key()
        return jwt.encode(claims, private_key, algorithm=self.jwt_alg, headers=headers)

    # ----- Summarizers (ported 1:1 from your script) -----

    @staticmethod
    def _strip_html(s: Optional[str]) -> str:
        return re.sub(r"<[^>]*>", "", s or "").strip()

    def _patient_summary(self, fhir_client: SyncFHIRClient, patient_id: str) -> Optional[Dict[str, Any]]:
        pats = fhir_client.resources("Patient").search(_id=patient_id).fetch()
        if not pats:
            return None
        pat = pats[0]
        name = (pat.get("name") or [{}])[0]
        full_name = (
            name.get("text")
            or " ".join(
                list(filter(None, [(name.get("given") or [""])[0], name.get("family")]))
            ).strip()
        )
        gp_display = None
        gps = pat.get("generalPractitioner") or []
        if gps:
            gp_display = (gps[0] or {}).get("display")
        return {
            "id": patient_id,
            "name": full_name,
            "dob": pat.get("birthDate"),
            "gender": pat.get("gender"),
            "general_practitioner": gp_display,
        }

    def _summarize_observation(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        based_on = ((obs.get("basedOn") or [{}])[0] or {}).get("display")
        code = obs.get("code") or {}
        code_text = code.get("text")
        vq = obs.get("valueQuantity") or {}
        val = vq.get("value")
        unit = vq.get("unit") or vq.get("code")
        issued = obs.get("issued")
        if issued:
            # Using dateutil (handles Zulu time automatically)
            try:
                issued = parser.isoparse(issued).strftime("%m/%d/%Y")
            except (parser.ParserError, TypeError):
                pass  # Keep original string if parsing fails
        text = f"{(based_on or '')}: {(code_text or '').replace(':','')}".strip()
        if val is not None:
            text = f"{text} {val}{(' ' + unit) if unit else ''} {issued or ''}".strip()
        else:
            text = f"{text} {issued or ''}".strip()
        return {
            "based_on": based_on,
            "code_text": code_text,
            "value": val,
            "unit": unit,
            "issued": issued,
            "text": text,
        }

    def _summarize_allergy(self, ai: Dict[str, Any]) -> Dict[str, Any]:
        code = ai.get("code") or {}
        code_text = code.get("text")
        coding = (code.get("coding") or [{}])
        display = coding[0].get("display") if coding else None
        narrative = ai.get("text") or {}
        narrative_text = self._strip_html(narrative.get("div")) if isinstance(narrative, dict) else None
        label = code_text or display or narrative_text or "Allergy/Intolerance (unspecified)"
        return {"text": label}

    def _summarize_condition(self, cond: Dict[str, Any]) -> Dict[str, Any]:
        code = cond.get("code") or {}
        label = code.get("text")
        if not label and (code.get("coding") or []):
            label = (code["coding"][0] or {}).get("display")
        return {"text": label or "Condition (unspecified)"}

    def _summarize_medication(self, ms: Dict[str, Any]) -> Dict[str, Any]:
        med_ref = (ms.get("medicationReference") or {})
        med_name = med_ref.get("display")
        dosage = (ms.get("dosage") or [{}])[0] or {}
        dose_text = dosage.get("text")
        return {
            "name": med_name or "Medication (unspecified)",
            "dosage": dose_text,
        }
# ----- CLI preserved (same behavior) -----
def main():
    try:
      svc = EpicFHIRService() 
      result = svc.snapshot(patient_id)
      print(json.dumps(result, indent=2))
    except Exception as e:
      print(json.dumps({"error": str(e)}))
      sys.exit(1)

if __name__ == "__main__":
    patient_id = EPIC_TEST_PATIENT_ID
    if not patient_id:
        print(json.dumps({"error": "Missing patient id. Use --patient or EPIC_TEST_PATIENT_ID"}))
        sys.exit(1)
    main()
