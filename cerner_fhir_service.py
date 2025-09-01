from __future__ import annotations

import os, sys
import re
from datetime import datetime
from dateutil import parser, relativedelta
import time
import uuid
import json
from typing import Any, Dict, List, Optional
import jwt
import requests
from dotenv import load_dotenv
import asyncio
from fhirpy import AsyncFHIRClient, SyncFHIRClient

load_dotenv()


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name, default)
    return v if (v is None or isinstance(v, str)) else str(v)


class CernerFHIRService:
    """
    Oracle Health (Cerner) FHIR R4 backend/system app helper using private_key_jwt.
    """

    def __init__(
        self,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        private_key_path: str | None = None,
        token_url: str | None = None,
        fhir_base: str | None = None,
        scope: str | None = None,
        auth_method: str | None = None,
        jwk_kid: str | None = None,
        jwt_alg: str = "RS384",
        timeout_s: int = 30,
        debug_token: bool = False,
        
    ) -> None:
        self.tenant_id = tenant_id or os.getenv("CERNER_TENANT_ID")
        self.client_id = client_id or os.getenv("CERNER_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("CERNER_CLIENT_SECRET")
        self.private_key_path = private_key_path or os.getenv("CERNER_PRIVATE_KEY_PATH")
        self.jwk_kid = jwk_kid or os.getenv("CERNER_JWK_KID")

        raw_scope = scope or os.getenv("CERNER_SCOPE") or ""
        self.scope = self._normalize_scope(raw_scope)

        self.timeout_s = timeout_s
        self.jwt_alg = jwt_alg
        self.debug_token = debug_token

        # Token URL: derive from tenant if not provided
        self.token_url = token_url or os.getenv("CERNER_TOKEN_URL") or (
            f"https://authorization.cerner.com/tenants/{self.tenant_id}/protocols/oauth2/profiles/smart-v1/token"
            if self.tenant_id else None
        )

        base = fhir_base or os.getenv("CERNER_FHIR_BASE")
        if not base:
            raise RuntimeError("Missing CERNER_FHIR_BASE (use the EHR base for system apps).")
        # Ensure trailing slash for fhirclient
        self.fhir_base = base if base.endswith("/") else base + "/"

        inferred = "private_key_jwt" if self.private_key_path else ("client_secret" if self.client_secret else None)
        self.auth_method = (auth_method or os.getenv("CERNER_AUTH_METHOD") or inferred or "private_key_jwt").lower()

        self._access_token: str | None = None
        self._token_exp: int | None = None

        self._check_min_config()

    # ----------------------------- Auth helpers -----------------------------

    @staticmethod
    def _normalize_scope(s: str) -> str:
        if not s:
            return ""
        s = s.strip()
        if s.startswith("CERNER_SCOPE="):
            s = s.split("=", 1)[1].strip()
        s = s.replace(",", " ")
        s = re.sub(r"\s+", " ", s)
        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            s = s[1:-1].strip()
        return s

    def _check_min_config(self) -> None:
        if not self.client_id:
            raise RuntimeError("Missing client_id (CERNER_CLIENT_ID).")
        if not self.token_url:
            raise RuntimeError("Missing token_url; set CERNER_TOKEN_URL or CERNER_TENANT_ID.")
        if not self.fhir_base:
            raise RuntimeError("Missing CERNER_FHIR_BASE.")
        if self.auth_method == "private_key_jwt" and not self.private_key_path:
            raise RuntimeError("auth_method=private_key_jwt requires CERNER_PRIVATE_KEY_PATH.")
        if self.auth_method == "client_secret" and not self.client_secret:
            raise RuntimeError("auth_method=client_secret requires CERNER_CLIENT_SECRET.")
        if not self.scope:
            raise RuntimeError(
                "No OAuth scopes configured. Set CERNER_SCOPE to a space-delimited list, e.g.:\n"
                "  system/Patient.read system/Observation.read ..."
            )

    def _load_private_key(self) -> str:
        with open(self.private_key_path, "r", encoding="utf-8") as f:
            return f.read()

    def _build_client_assertion(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self.client_id,
            "sub": self.client_id,
            "aud": self.token_url,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "nbf": now,
            "exp": now + 180,
        }
        headers = {"alg": self.jwt_alg, "typ": "JWT"}
        if self.jwk_kid:
            headers["kid"] = self.jwk_kid
        return jwt.encode(claims, self._load_private_key(), algorithm=self.jwt_alg, headers=headers)

    def _get_access_token(self, force: bool = False) -> dict:
        now = int(time.time())
        if self._access_token and self._token_exp and not force and now < (self._token_exp - 15):
            return {
                "access_token": self._access_token,
                "token_type": "Bearer",
                "expires_in": max(0, self._token_exp - now),
                "scope": self.scope,
            }

        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        scope_str = self._normalize_scope(self.scope)
        if not scope_str:
            raise RuntimeError("Refusing to request token with empty scope (after normalization).")

        if self.auth_method == "private_key_jwt":
            data = {
                "grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": self._build_client_assertion(),
                "scope": scope_str,
            }
            resp = requests.post(self.token_url, headers=headers, data=data, timeout=self.timeout_s)
        else:
            data = {"grant_type": "client_credentials", "scope": scope_str}
            resp = requests.post(self.token_url, headers=headers, data=data,
                                 auth=(self.client_id, self.client_secret), timeout=self.timeout_s)

        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            srv_date = resp.headers.get("Date")
            if srv_date:
                print(f"[cerner] Server Date: {srv_date}")
            print(f"[cerner] Local UTC:   {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
            raise RuntimeError(f"Token request failed ({resp.status_code}): {resp.text}") from e

        tok = resp.json()
        self._access_token = tok.get("access_token")
        if not self._access_token:
            raise RuntimeError(f"Token response missing access_token: {tok}")
        self._token_exp = int(time.time()) + int(tok.get("expires_in", 0))
        return tok

    # ----------------------------- FHIR client -----------------------------
    
    def build_fhir_client(self, base_url: str, access_token: str) -> SyncFHIRClient:
        """
        Build a fhirpy SyncFHIRClient with the Bearer token attached.
        """
        return SyncFHIRClient(base_url, authorization=f"Bearer {access_token}")
    
     # --------------------------- Public API --------------------------------
    
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
        limit = os.getenv("CERNER_SET_LIMIT", "0")
        
        # 1) Get token
        token_resp = self._get_access_token()
        access_token = token_resp["access_token"]

        # 2) FHIR clients
        fhir = self.build_fhir_client(self.fhir_base, access_token)

        # 3) Patient await
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
        allergies_list = allergies_list[:10]

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
             patient=patient_id, category="encounter-diagnosis", clinicalStatus="active", lastUpdated=f"ge{(now - years_past).isoformat()}"
            ).limit(10).fetch()
        else:
             conditions_raw = fhir.resources("Condition").search(
             patient=patient_id, category="encounter-diagnosis", clinicalStatus="active", lastUpdated=f"ge{(now - years_past).isoformat()}"
            ).fetch_all()
        conditions_list = [self._summarize_condition(c) for c in (conditions_raw or [])]
        conditions_list = conditions_list[:10]

        # De-duplicate conditions
        seen_condition_texts = set()
        conditions = []
        for cond in conditions_list:
            if cond['text'] not in seen_condition_texts:
                conditions.append(cond)
                seen_condition_texts.add(cond['text'])

        # 7) Medications
        if limit == "1":
            meds_raw = fhir.resources("MedicationRequest").search(
             patient=patient_id, status="active", intent="order"
            ).limit(10).fetch()
        else:
            meds_raw = fhir.resources("MedicationRequest").search(
             patient=patient_id, status="active", intent="order"
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
    # ------------------------- Formatting helpers --------------------------

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

    @staticmethod
    def _summarize_observation(obs: Dict[str, Any]) -> Dict[str, Any]:
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
        text = f"{(code_text or '').replace(':','')}".strip()
        if val is not None:
            text = f"{text} {val}{(' ' + unit) if unit else ''} {issued or ''}".strip()
        else:
            text = f"{text} {issued or ''}".strip()
        return {
            "code_text": code_text,
            "value": val,
            "unit": unit,
            "issued": issued,
            "text": text,
        }

    @staticmethod
    def _summarize_allergy(ai: Dict[str, Any]) -> Dict[str, Any]:
        code = ai.get("code") or {}
        code_text = code.get("text")
        coding = code.get("coding") or []
        display = (coding[0] or {}).get("display") if coding else None
        narrative = ai.get("text") or {}
        narrative_text = CernerFHIRService._strip_html(narrative.get("div")) if isinstance(narrative, dict) else None
        label = code_text or display or narrative_text or "Allergy/Intolerance (unspecified)"
        return {"text": label}

    @staticmethod
    def _summarize_condition(cond: Dict[str, Any]) -> Dict[str, Any]:
        code = cond.get("code") or {}
        label = code.get("text")
        if not label and (code.get("coding") or []):
            label = (code["coding"][0] or {}).get("display")
        return {"text": label or "Condition (unspecified)"}

    @staticmethod
    def _summarize_medication(mr: Dict[str, Any]) -> Dict[str, Any]:
        med_cc = mr.get("medicationCodeableConcept") or {}
        med = med_cc.get("text")
        di = (mr.get("dosageInstruction") or [{}])[0] or {}
        dose_text = di.get("text")
        return {"name": med or "Medication (unspecified)", "dosage": dose_text}

# ------------------------------ CLI ----------------------------------------
def main():
    try:
        svc = CernerFHIRService()
        result = svc.snapshot(patient_id)
        with open("patient_data.json", "w") as file:
          file.write(json.dumps(result, indent=2))
        print(result)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    patient_id = _env("CERNER_TEST_PATIENT_ID")
    if not patient_id:
        print(json.dumps({"error": "Missing patient id. Use --patient or CERNER_TEST_PATIENT_ID"}))
        sys.exit(1)
    main()
