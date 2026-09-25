"""Google Sheets client for HiringFlow AI Simulator.

Reads from Heads sheet (internal users), Candidates sheet (test call candidates),
Classifications sheet, and AI Clients sheet.
Google Sheets is the single source of truth.
"""

import os
import secrets
import uuid
import logging
from typing import Optional, Dict, Any

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger("sheets")


def _parse_candidate_datetime(raw: str):
    """Parse a TestCallScheduledAt cell value into a timezone-aware UTC datetime.

    The external HiringFlow-side platform writes this cell as plain text with no
    timezone marker — assumed to be Africa/Cairo local time (matching this app's
    other timezone convention, see agent.py's daily pricing refresh), then
    converted to UTC for comparison. Returns None if empty/unparseable.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        from dateutil import parser as date_parser
        dt = date_parser.parse(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Africa/Cairo"))
    return dt.astimezone(ZoneInfo("UTC"))

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]


def load_google_credentials(scopes: list[str]) -> Credentials:
    """Build service-account Credentials from either GOOGLE_CREDENTIALS_JSON (the
    key file's content, for hosts like SnapDeploy where you can't drop a file
    next to the code) or GOOGLE_CREDENTIALS (a local file path, for local dev).
    """
    creds_json = os.getenv('GOOGLE_CREDENTIALS_JSON', '').strip()
    if creds_json:
        import json
        try:
            info = json.loads(creds_json)
        except ValueError as e:
            raise ValueError(f"GOOGLE_CREDENTIALS_JSON is not valid JSON: {e}") from e
        return Credentials.from_service_account_info(info, scopes=scopes)

    creds_path = os.getenv('GOOGLE_CREDENTIALS', '').strip()
    if creds_path:
        return Credentials.from_service_account_file(creds_path, scopes=scopes)

    raise ValueError(
        "Either GOOGLE_CREDENTIALS_JSON (key file content) or GOOGLE_CREDENTIALS "
        "(local key file path) environment variable is required"
    )

# Column indices for Candidates sheet (1-indexed).
#
# This sheet's own row is owned/written by the external HiringFlow-side platform
# (candidate creation, booking, approvals, offer pipeline) EXCEPT the "our"
# columns below, which the AI Simulator owns and writes:
#   TestCallLink, TestCallToken, TestSessionId, TestCall Status,
#   TestCallStartedAt, TestCallEndedAt, TestCallScore, TestCallResult,
#   AudioRecordingUrl, DriveFolderId, TestCallEvaluation, RegenerationCount
# TestCallLinkSentAt is explicitly NOT written by us — an external Apps Script
# trigger stamps it after emailing the candidate the link we generated.
#
# Actual live column order (confirmed 2026-09-18):
# A=CreatedAt, B=DriveFolderId, C=CandidateID, D=CandidateName, E=CandidateEmail,
# F=PhoneNumber, G=CVUrl, H=Queue, I=Batch, J=SubBatch, K=TAEmail,
# L=AcceptanceEmailScheduledAt, M=AcceptanceEmailSentAt,
# N=BookingWindowStart, O=BookingWindowEnd, P=TestCallScheduledAt,
# Q=RescheduleCount, R=TestCallLink, S=TestCallToken, T=TestSessionId,
# U=TestCallLinkSentAt, V=TestCallStartedAt, W=TestCallEndedAt, X=TestCallScore,
# Y=TestCallResult, Z=AudioRecordingUrl, AA=TestCallEvaluation,
# AB="TestCall Status" (note the space — not "TestCallStatus"), AC=TestCallDisputeStatus,
# AD=DisputeDecisionNotes, AE=HRTAApproval, AF=HeadOfSalesApproval,
# AG=QualityApproval, AH=TATeamLeaderApproval, AI=TAManagerNotification,
# AJ=OfferSentAt, AK=OfferStatus, AL=OfferDecision, AM=FinalCandidateStatus,
# AN=PreviousTestCallScore, AO=PreviousTestCallResult, AP=PreviousTestCallEvaluation,
# AQ=RegenerationCount
CANDIDATE_COLS = {
    'created_at': 1,                      # A
    'drive_folder_id': 2,                 # B
    'candidate_id': 3,                    # C
    'candidate_name': 4,                  # D
    'candidate_email': 5,                 # E
    'phone_number': 6,                    # F
    'cv_url': 7,                          # G
    'queue': 8,                           # H
    'batch': 9,                           # I
    'sub_batch': 10,                      # J
    'ta_email': 11,                       # K
    'acceptance_email_scheduled_at': 12,  # L
    'acceptance_email_sent_at': 13,       # M
    'booking_window_start': 14,           # N
    'booking_window_end': 15,             # O
    'test_call_scheduled_at': 16,         # P
    'reschedule_count': 17,               # Q
    'test_call_link': 18,                 # R
    'test_call_token': 19,                # S
    'test_session_id': 20,                # T
    'test_call_link_sent_at': 21,         # U — owned by the external Apps Script, never written by us
    'test_call_started_at': 22,           # V
    'test_call_ended_at': 23,             # W
    'test_call_score': 24,                # X
    'test_call_result': 25,               # Y
    'audio_recording_url': 26,            # Z
    'test_call_evaluation': 27,           # AA
    'test_call_status': 28,               # AB — header text is "TestCall Status" (with a space)
    'dispute_status': 29,                 # AC — header text is "TestCallDisputeStatus"
    'dispute_decision_notes': 30,         # AD
    'hr_ta_approval': 31,                 # AE
    'head_of_sales_approval': 32,         # AF
    'quality_approval': 33,               # AG
    'ta_team_leader_approval': 34,        # AH
    'ta_manager_notification': 35,        # AI
    'offer_sent_at': 36,                  # AJ
    'offer_status': 37,                   # AK
    'offer_decision': 38,                 # AL
    'final_candidate_status': 39,         # AM
    'previous_test_call_score': 40,       # AN
    'previous_test_call_result': 41,      # AO
    'previous_test_call_evaluation': 42,  # AP
    'regeneration_count': 43,             # AQ
}

# The Candidates sheet's actual header STRINGS don't all match a simple
# PascalCase(key) transform (e.g. "TestCall Status" has a space, "dispute_status"
# maps to header "TestCallDisputeStatus") — this maps our internal keys to the
# exact header text, for both reading (row.get(...)) and tab creation.
CANDIDATE_HEADER_NAMES = {
    'created_at': 'CreatedAt',
    'drive_folder_id': 'DriveFolderId',
    'candidate_id': 'CandidateID',
    'candidate_name': 'CandidateName',
    'candidate_email': 'CandidateEmail',
    'phone_number': 'PhoneNumber',
    'cv_url': 'CVUrl',
    'queue': 'Queue',
    'batch': 'Batch',
    'sub_batch': 'SubBatch',
    'ta_email': 'TAEmail',
    'acceptance_email_scheduled_at': 'AcceptanceEmailScheduledAt',
    'acceptance_email_sent_at': 'AcceptanceEmailSentAt',
    'booking_window_start': 'BookingWindowStart',
    'booking_window_end': 'BookingWindowEnd',
    'test_call_scheduled_at': 'TestCallScheduledAt',
    'reschedule_count': 'RescheduleCount',
    'test_call_link': 'TestCallLink',
    'test_call_token': 'TestCallToken',
    'test_session_id': 'TestSessionId',
    'test_call_link_sent_at': 'TestCallLinkSentAt',
    'test_call_started_at': 'TestCallStartedAt',
    'test_call_ended_at': 'TestCallEndedAt',
    'test_call_score': 'TestCallScore',
    'test_call_result': 'TestCallResult',
    'audio_recording_url': 'AudioRecordingUrl',
    'test_call_evaluation': 'TestCallEvaluation',
    'test_call_status': 'TestCall Status',
    'dispute_status': 'TestCallDisputeStatus',
    'dispute_decision_notes': 'DisputeDecisionNotes',
    'hr_ta_approval': 'HRTAApproval',
    'head_of_sales_approval': 'HeadOfSalesApproval',
    'quality_approval': 'QualityApproval',
    'ta_team_leader_approval': 'TATeamLeaderApproval',
    'ta_manager_notification': 'TAManagerNotification',
    'offer_sent_at': 'OfferSentAt',
    'offer_status': 'OfferStatus',
    'offer_decision': 'OfferDecision',
    'final_candidate_status': 'FinalCandidateStatus',
    'previous_test_call_score': 'PreviousTestCallScore',
    'previous_test_call_result': 'PreviousTestCallResult',
    'previous_test_call_evaluation': 'PreviousTestCallEvaluation',
    'regeneration_count': 'RegenerationCount',
}


class GoogleSheetsClient:
    """Client for reading/writing HiringFlow Google Sheets."""

    def __init__(self):
        self.sheet_id = os.getenv('GOOGLE_SHEET_ID', '').strip()
        if not self.sheet_id:
            raise ValueError("GOOGLE_SHEET_ID environment variable is required")

        creds = load_google_credentials(SCOPES)
        self.client = gspread.authorize(creds)
        self.sheet = self.client.open_by_key(self.sheet_id)

    HEADS_HEADERS = [
        'Name', 'Email', 'Queue', 'Status', 'Role', 'AccessCode',
        'Permissions', 'AllClassifications', 'ClassificationIDs',
    ]

    def _ensure_heads_tab(self):
        """Create Heads tab if it doesn't exist. Also migrate old headers."""
        try:
            ws = self.sheet.worksheet('Heads')
            existing = ws.row_values(1)
            missing = [h for h in self.HEADS_HEADERS if h not in existing]
            if missing:
                logger.info("Migrating Heads tab: adding %s", missing)
                start_col = len(existing) + 1
                if ws.col_count < start_col + len(missing) - 1:
                    ws.add_cols(start_col + len(missing) - 1 - ws.col_count)
                for i, header in enumerate(missing):
                    ws.update_cell(1, start_col + i, header)
                # المستخدمين الحاليين (قبل النظام ده): صلاحيات كاملة + كل التصنيفات،
                # عشان محدش يفقد وصوله فجأة لما الأعمدة دي تتضاف.
                if 'Permissions' in missing or 'AllClassifications' in missing:
                    num_rows = len(ws.get_all_values())
                    perm_col = self.HEADS_HEADERS.index('Permissions') + 1
                    all_col = self.HEADS_HEADERS.index('AllClassifications') + 1
                    full_perms = ','.join(k for k in ['manage_users', 'manage_classifications',
                                                       'manage_ai_clients', 'delete_calls_results',
                                                       'view_results', 'view_recordings',
                                                       'generate_test_links', 'make_calls'])
                    for r in range(2, num_rows + 1):
                        ws.update_cell(r, perm_col, full_perms)
                        ws.update_cell(r, all_col, 'true')
                logger.info("Migration complete: Heads tab now has %s", ws.row_values(1))
        except gspread.exceptions.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title='Heads', rows=100, cols=len(self.HEADS_HEADERS))
            ws.append_row(self.HEADS_HEADERS)
            logger.info("Created Heads tab")

    def _ensure_candidates_tab(self):
        """Create Candidates tab if it doesn't exist. Header order/names mirror
        CANDIDATE_COLS/CANDIDATE_HEADER_NAMES exactly — the real sheet is owned
        mostly by the external HiringFlow-side platform, so this only matters for
        a brand-new tab (existing sheets are never auto-migrated column-by-column
        here, since positions carry real external data)."""
        try:
            self.sheet.worksheet('Candidates')
        except gspread.exceptions.WorksheetNotFound:
            ordered_keys = sorted(CANDIDATE_COLS, key=lambda k: CANDIDATE_COLS[k])
            headers = [CANDIDATE_HEADER_NAMES[k] for k in ordered_keys]
            ws = self.sheet.add_worksheet(title='Candidates', rows=100, cols=len(headers))
            ws.append_row(headers)
            logger.info("Created Candidates tab")

    def get_heads_user(self, email: str, access_code: str) -> Optional[Dict[str, Any]]:
        """Lookup internal user from Heads sheet.

        Returns user dict with name, email, queue, role if found and active.
        Returns None if not found or inactive.
        """
        try:
            self._ensure_heads_tab()
            heads = self.sheet.worksheet('Heads')
            records = heads.get_all_records()

            for row in records:
                row_email = str(row.get('Email', '')).strip().lower()
                row_code = str(row.get('AccessCode', '')).strip()
                row_status = str(row.get('Status', '')).strip().lower()

                if (row_email == email.lower().strip() and
                    row_code == access_code.strip() and
                    row_status == 'active'):
                    return self._parse_heads_permissions(row)
            return None
        except Exception as e:
            logger.error("Failed to read Heads sheet: %s", e)
            return None

    @staticmethod
    def _parse_heads_permissions(row: dict) -> Dict[str, Any]:
        """يحوّل صف من شيت Heads لبيانات مستخدم كاملة (صلاحيات + تصنيفات مسموحة)."""
        perms_raw = str(row.get('Permissions', '') or '')
        granted = {p.strip() for p in perms_raw.split(',') if p.strip()}
        classification_ids_raw = str(row.get('ClassificationIDs', '') or '')
        return {
            'name': row.get('Name', ''),
            'email': row.get('Email', ''),
            'queue': row.get('Queue', ''),
            'role': row.get('Role', ''),
            'permissions': {key: (key in granted) for key in [
                'manage_users', 'manage_classifications', 'manage_ai_clients',
                'delete_calls_results', 'view_results', 'view_recordings',
                'generate_test_links', 'make_calls',
            ]},
            'all_classifications': str(row.get('AllClassifications', '')).strip().lower() == 'true',
            'classification_ids': [c.strip() for c in classification_ids_raw.split(',') if c.strip()],
        }

    def get_all_heads_users(self) -> list[Dict[str, Any]]:
        """Get all users from the Heads sheet."""
        try:
            self._ensure_heads_tab()
            heads = self.sheet.worksheet('Heads')
            records = heads.get_all_records()
            results = []
            for row in records:
                email = str(row.get('Email', '')).strip()
                if not email:
                    continue
                parsed = self._parse_heads_permissions(row)
                results.append({
                    'username': email,
                    'name': parsed['name'],
                    'email': email,
                    'role': parsed['role'],
                    'queue': row.get('Queue', ''),
                    'status': str(row.get('Status', '')).strip().lower(),
                    'permissions': parsed['permissions'],
                    'all_classifications': parsed['all_classifications'],
                    'classification_ids': parsed['classification_ids'],
                })
            return results
        except Exception as e:
            logger.error("Failed to read Heads sheet: %s", e)
            return []

    def create_heads_user(
        self, email: str, name: str, role: str, access_code: str,
        permissions: Optional[Dict[str, bool]] = None,
        all_classifications: bool = False,
        classification_ids: Optional[list[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a new user in the Heads sheet."""
        try:
            self._ensure_heads_tab()
            heads = self.sheet.worksheet('Heads')
            # Check if email already exists
            records = heads.get_all_records()
            for row in records:
                if str(row.get('Email', '')).strip().lower() == email.lower():
                    return None  # Already exists
            perms_str = ','.join(k for k, v in (permissions or {}).items() if v)
            classifications_str = ','.join(classification_ids or [])
            heads.append_row([
                name, email, '', 'active', role, access_code,
                perms_str, str(bool(all_classifications)).lower(), classifications_str,
            ])
            logger.info("Created Heads user: %s (%s)", email, name)
            return {
                'username': email, 'name': name, 'email': email, 'role': role,
                'permissions': {k: bool(v) for k, v in (permissions or {}).items()},
                'all_classifications': bool(all_classifications),
                'classification_ids': classification_ids or [],
            }
        except Exception as e:
            logger.error("Failed to create Heads user: %s", e)
            return None

    def update_heads_user(self, email: str, data: Dict[str, Any]) -> bool:
        """يعدّل مستخدم موجود (الاسم، الدور، كلمة المرور، الحالة، الصلاحيات، التصنيفات
        المسموحة، أو حتى البريد نفسه). أي حقل مش موجود في data يُترك كما هو."""
        try:
            self._ensure_heads_tab()
            heads = self.sheet.worksheet('Heads')
            records = heads.get_all_records()
            for i, row in enumerate(records, start=2):
                if str(row.get('Email', '')).strip().lower() == email.lower():
                    if 'name' in data and data['name']:
                        heads.update_cell(i, 1, data['name'])
                    if 'email' in data and data['email']:
                        heads.update_cell(i, 2, data['email'])
                    if 'status' in data and data['status']:
                        heads.update_cell(i, 4, data['status'])
                    if 'role' in data and data['role']:
                        heads.update_cell(i, 5, data['role'])
                    if 'password' in data and data['password']:
                        heads.update_cell(i, 6, data['password'])
                    if 'permissions' in data and data['permissions'] is not None:
                        perms_str = ','.join(k for k, v in data['permissions'].items() if v)
                        heads.update_cell(i, 7, perms_str)
                    if 'all_classifications' in data:
                        heads.update_cell(i, 8, str(bool(data['all_classifications'])).lower())
                    if 'classification_ids' in data and data['classification_ids'] is not None:
                        heads.update_cell(i, 9, ','.join(data['classification_ids']))
                    logger.info("Updated Heads user: %s", email)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to update Heads user: %s", e)
            return False

    def delete_heads_user(self, email: str) -> bool:
        """Delete a user from the Heads sheet by email."""
        try:
            self._ensure_heads_tab()
            heads = self.sheet.worksheet('Heads')
            records = heads.get_all_records()
            for i, row in enumerate(records, start=2):
                if str(row.get('Email', '')).strip().lower() == email.lower():
                    heads.delete_rows(i, i)
                    logger.info("Deleted Heads user: %s", email)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to delete Heads user: %s", e)
            return False

    @staticmethod
    def _parse_candidate_row(row: dict) -> Dict[str, Any]:
        """Parse a Candidates sheet row (as returned by get_all_records) into our dict shape.
        Uses CANDIDATE_HEADER_NAMES so it stays correct even though several headers
        don't map to a simple PascalCase(key) transform (e.g. "TestCall Status")."""
        def h(key: str, default=''):
            return row.get(CANDIDATE_HEADER_NAMES[key], default)

        return {
            'candidate_id': h('candidate_id'),
            'candidate_name': h('candidate_name'),
            'candidate_email': h('candidate_email'),
            'scenario': 'new-lead-discovery-call',  # Default scenario
            'test_call_status': h('test_call_status', 'pending') or 'pending',
            'test_call_started_at': h('test_call_started_at'),
            'test_call_ended_at': h('test_call_ended_at'),
            'score': h('test_call_score'),
            'result': h('test_call_result'),
            'regeneration_count': int(h('regeneration_count', 0) or 0),
            'test_session_id': h('test_session_id'),
            'queue': h('queue'),
            'audio_recording_url': h('audio_recording_url'),
            'drive_folder_id': h('drive_folder_id'),
            'test_call_link': h('test_call_link'),
            'test_call_token': h('test_call_token'),
            'test_call_scheduled_at': h('test_call_scheduled_at'),
            'reschedule_count': int(h('reschedule_count', 0) or 0),
        }

    def get_candidate(self, email: str, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Lookup candidate from Candidates sheet by email + candidate ID.

        Returns candidate dict if found.
        Returns None if not found.
        """
        try:
            self._ensure_candidates_tab()
            candidates = self.sheet.worksheet('Candidates')
            records = candidates.get_all_records()

            for row in records:
                row_email = str(row.get(CANDIDATE_HEADER_NAMES['candidate_email'], '')).strip().lower()
                row_id = str(row.get(CANDIDATE_HEADER_NAMES['candidate_id'], '')).strip()

                if row_email == email.lower().strip() and row_id == candidate_id.strip():
                    return self._parse_candidate_row(row)
            return None
        except Exception as e:
            logger.error("Failed to read Candidates sheet: %s", e)
            return None

    def get_candidate_by_id(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Lookup candidate from Candidates sheet by ID only (no email required) —
        used for the token-based auto-generated test-call link, where the candidate
        never has to type their email."""
        try:
            self._ensure_candidates_tab()
            candidates = self.sheet.worksheet('Candidates')
            records = candidates.get_all_records()
            for row in records:
                row_id = str(row.get(CANDIDATE_HEADER_NAMES['candidate_id'], '')).strip()
                if row_id == candidate_id.strip():
                    return self._parse_candidate_row(row)
            return None
        except Exception as e:
            logger.error("Failed to read Candidates sheet by id: %s", e)
            return None

    def get_candidates_needing_test_link(self) -> list[Dict[str, Any]]:
        """Candidates whose TestCallScheduledAt is within the next hour (or already
        passed) but who don't have a TestCallLink yet. Used by the background
        poller (agent.py) that auto-generates links ahead of the appointment.

        Skips candidates whose RescheduleCount has already reached/exceeded the
        admin-configured MaxRescheduleCount (Link Settings tab) — this is what
        stops a link being regenerated forever after the other platform clears
        TestCallLink/TestCallToken for a reschedule beyond the allowed limit."""
        from datetime import datetime, timezone, timedelta
        try:
            self._ensure_candidates_tab()
            ws = self.sheet.worksheet('Candidates')
            records = ws.get_all_records()
            results = []
            now = datetime.now(timezone.utc)
            max_reschedules = self.get_max_reschedule_count()
            for row in records:
                candidate_id = str(row.get(CANDIDATE_HEADER_NAMES['candidate_id'], '')).strip()
                if not candidate_id:
                    continue
                scheduled_raw = str(row.get(CANDIDATE_HEADER_NAMES['test_call_scheduled_at'], '')).strip()
                if not scheduled_raw:
                    continue
                if str(row.get(CANDIDATE_HEADER_NAMES['test_call_link'], '')).strip():
                    continue  # already has a link
                status = str(row.get(CANDIDATE_HEADER_NAMES['test_call_status'], '')).strip().lower()
                if status not in ('', 'pending', 'not_sent'):
                    continue  # started/ended/expired — not eligible for a fresh link
                if max_reschedules >= 0:
                    try:
                        reschedule_count = int(row.get(CANDIDATE_HEADER_NAMES['reschedule_count'], 0) or 0)
                    except (TypeError, ValueError):
                        reschedule_count = 0
                    if reschedule_count >= max_reschedules:
                        logger.info(
                            "Candidate %s reached MaxRescheduleCount (%d) — no further link will be generated",
                            candidate_id, max_reschedules,
                        )
                        continue
                scheduled_at = _parse_candidate_datetime(scheduled_raw)
                if not scheduled_at:
                    logger.warning("Candidate %s has an unparseable TestCallScheduledAt: %r", candidate_id, scheduled_raw)
                    continue
                if now >= scheduled_at - timedelta(hours=1):
                    results.append(self._parse_candidate_row(row))
            return results
        except Exception as e:
            logger.error("Failed to get candidates needing a test link: %s", e)
            return []

    def generate_test_call_link(self, candidate_id: str, base_url: str) -> Optional[str]:
        """Generate a secure one-time test-call link for a candidate and write the
        link + its token into that candidate's own Candidates row. Deliberately
        does NOT touch TestCallLinkSentAt — an external Apps Script trigger owns
        stamping that after it emails the candidate the link."""
        try:
            self._ensure_candidates_tab()
            ws = self.sheet.worksheet('Candidates')
            records = ws.get_all_records()
            for i, row in enumerate(records, start=2):
                if str(row.get(CANDIDATE_HEADER_NAMES['candidate_id'], '')).strip() == candidate_id.strip():
                    token = secrets.token_urlsafe(32)
                    link = f"{base_url.rstrip('/')}/?candidate={candidate_id}&token={token}"
                    ws.update_cell(i, CANDIDATE_COLS['test_call_link'], link)
                    ws.update_cell(i, CANDIDATE_COLS['test_call_token'], token)
                    logger.info("Generated test call link for candidate %s", candidate_id)
                    return link
            return None
        except Exception as e:
            logger.error("Failed to generate test call link for %s: %s", candidate_id, e)
            return None

    def validate_candidate_link(self, candidate_id: str, token: str) -> Dict[str, Any]:
        """Validate a token-based test-call link (the auto-generated, schedule-based
        flow — distinct from the manual "Generate Link" tab). Returns
        {'ok': True, 'candidate': {...}} or {'ok': False, 'reason': 'invalid'|'used'|'expired'}.
        Marks TestCall Status as 'Expired' the first time it's discovered that the
        scheduled time + 1 hour has passed with the link still unused."""
        from datetime import datetime, timezone, timedelta
        try:
            self._ensure_candidates_tab()
            ws = self.sheet.worksheet('Candidates')
            records = ws.get_all_records()
            for i, row in enumerate(records, start=2):
                row_id = str(row.get(CANDIDATE_HEADER_NAMES['candidate_id'], '')).strip()
                if row_id != candidate_id.strip():
                    continue
                row_token = str(row.get(CANDIDATE_HEADER_NAMES['test_call_token'], '')).strip()
                if not row_token or not token or row_token != token.strip():
                    return {'ok': False, 'reason': 'invalid'}
                status = str(row.get(CANDIDATE_HEADER_NAMES['test_call_status'], '')).strip().lower()
                if status in ('started', 'ended'):
                    return {'ok': False, 'reason': 'used'}
                if status == 'expired':
                    return {'ok': False, 'reason': 'expired'}
                scheduled_raw = str(row.get(CANDIDATE_HEADER_NAMES['test_call_scheduled_at'], '')).strip()
                scheduled_at = _parse_candidate_datetime(scheduled_raw) if scheduled_raw else None
                if scheduled_at and datetime.now(timezone.utc) > scheduled_at + timedelta(hours=1):
                    ws.update_cell(i, CANDIDATE_COLS['test_call_status'], 'Expired')
                    logger.info("Test call link for candidate %s expired unused", candidate_id)
                    return {'ok': False, 'reason': 'expired'}
                return {'ok': True, 'candidate': self._parse_candidate_row(row)}
            return {'ok': False, 'reason': 'invalid'}
        except Exception as e:
            logger.error("Failed to validate candidate link for %s: %s", candidate_id, e)
            return {'ok': False, 'reason': 'error'}

    def update_candidate_status(self, candidate_id: str, status: str, timestamp: Optional[str] = None) -> bool:
        """Update candidate test call status in Google Sheets.

        Args:
            candidate_id: The candidate's ID
            status: New status (pending, started, ended)
            timestamp: Optional timestamp to write

        Returns:
            True if updated, False if candidate not found
        """
        try:
            self._ensure_candidates_tab()
            candidates = self.sheet.worksheet('Candidates')
            records = candidates.get_all_records()

            for i, row in enumerate(records, start=2):  # start=2 for header row
                if str(row.get('CandidateID', '')).strip() == candidate_id.strip():
                    # Update status
                    candidates.update_cell(i, CANDIDATE_COLS['test_call_status'], status)

                    # Update timestamp if provided
                    if timestamp:
                        if status == 'started':
                            col = CANDIDATE_COLS['test_call_started_at']
                        else:
                            col = CANDIDATE_COLS['test_call_ended_at']
                        candidates.update_cell(i, col, timestamp)

                    logger.info("Updated candidate %s status to %s", candidate_id, status)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to update candidate status: %s", e)
            return False

    def update_candidate_result(self, candidate_id: str, score: int, result: str) -> bool:
        """Update candidate score and result in Google Sheets.

        Args:
            candidate_id: The candidate's ID
            score: Evaluation score
            result: PASSED or REJECTED

        Returns:
            True if updated, False if candidate not found
        """
        try:
            self._ensure_candidates_tab()
            candidates = self.sheet.worksheet('Candidates')
            records = candidates.get_all_records()

            for i, row in enumerate(records, start=2):
                if str(row.get('CandidateID', '')).strip() == candidate_id.strip():
                    candidates.update_cell(i, CANDIDATE_COLS['test_call_score'], score)
                    candidates.update_cell(i, CANDIDATE_COLS['test_call_result'], result)
                    logger.info("Updated candidate %s result: score=%d, result=%s", candidate_id, score, result)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to update candidate result: %s", e)
            return False

    def update_candidate_evaluation(self, candidate_id: str, evaluation: str) -> bool:
        """Update candidate evaluation in Google Sheets.

        Args:
            candidate_id: The candidate's ID
            evaluation: Evaluation JSON string

        Returns:
            True if updated, False if candidate not found
        """
        try:
            self._ensure_candidates_tab()
            candidates = self.sheet.worksheet('Candidates')
            records = candidates.get_all_records()

            for i, row in enumerate(records, start=2):
                if str(row.get('CandidateID', '')).strip() == candidate_id.strip():
                    candidates.update_cell(i, CANDIDATE_COLS['test_call_evaluation'], evaluation)
                    logger.info("Updated candidate %s evaluation", candidate_id)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to update candidate evaluation: %s", e)
            return False

    def update_candidate_recordings(self, candidate_id: str, drive_folder_id: str = '', audio_url: str = '', video_url: str = '') -> bool:
        """Update candidate recording URLs and Drive folder ID in Google Sheets.

        Args:
            candidate_id: The candidate's ID
            drive_folder_id: Google Drive folder ID
            audio_url: Audio recording URL
            video_url: Video recording URL

        Returns:
            True if updated, False if candidate not found
        """
        try:
            self._ensure_candidates_tab()
            candidates = self.sheet.worksheet('Candidates')
            records = candidates.get_all_records()

            for i, row in enumerate(records, start=2):
                if str(row.get('CandidateID', '')).strip() == candidate_id.strip():
                    if drive_folder_id:
                        candidates.update_cell(i, CANDIDATE_COLS['drive_folder_id'], drive_folder_id)
                    candidates.update_cell(i, CANDIDATE_COLS['audio_recording_url'], audio_url)
                    # No VideoRecordingUrl column on the Candidates sheet anymore — video_url is
                    # accepted for backward compatibility with callers but has nowhere to go.
                    logger.info("Updated candidate %s recording URLs + folder ID", candidate_id)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to update candidate recordings: %s", e)
            return False

    def increment_regeneration(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Create new candidate row for regeneration, preserve old attempt.

        Returns:
            New candidate data dict, or None if failed
        """
        try:
            self._ensure_candidates_tab()
            candidates = self.sheet.worksheet('Candidates')
            records = candidates.get_all_records()

            old_row_num = None
            old_candidate = None

            for i, row in enumerate(records, start=2):
                if str(row.get('CandidateID', '')).strip() == candidate_id.strip():
                    old_row_num = i
                    old_candidate = row
                    break

            if not old_candidate or not old_row_num:
                logger.error("Candidate %s not found for regeneration", candidate_id)
                return None

            # Increment regeneration count
            old_count = int(old_candidate.get('RegenerationCount', 0) or 0)
            new_count = old_count + 1
            candidates.update_cell(old_row_num, CANDIDATE_COLS['regeneration_count'], new_count)

            # Create new candidate row. Built as a dict keyed by our internal names and
            # materialized in the sheet's real column order, so this can't silently
            # drift out of alignment if the sheet's columns are ever reordered again.
            new_candidate_id = f"CAND-{uuid.uuid4().hex[:8].upper()}"
            new_values = {
                'candidate_id': new_candidate_id,
                'candidate_name': old_candidate.get('CandidateName', ''),
                'candidate_email': old_candidate.get('CandidateEmail', ''),
                'phone_number': old_candidate.get('PhoneNumber', ''),
                'queue': old_candidate.get('Queue', ''),
                'batch': old_candidate.get('Batch', ''),
                'sub_batch': old_candidate.get('SubBatch', ''),
                'ta_email': old_candidate.get('TAEmail', ''),
                'reschedule_count': 0,
                'test_call_status': 'pending',
                'regeneration_count': new_count,
                'test_session_id': f"TEST-{uuid.uuid4().hex[:8].upper()}",
            }
            ordered_keys = sorted(CANDIDATE_COLS, key=lambda k: CANDIDATE_COLS[k])
            new_row = [new_values.get(k, '') for k in ordered_keys]

            candidates.append_row(new_row)

            logger.info("Regenerated candidate: %s -> %s (attempt %d)", candidate_id, new_candidate_id, new_count + 1)

            return {
                'candidate_id': new_candidate_id,
                'candidate_name': old_candidate.get('CandidateName', ''),
                'candidate_email': old_candidate.get('CandidateEmail', ''),
                'scenario': 'new-lead-discovery-call',
                'status': 'pending',
                'attempt_number': new_count + 1,
                'regeneration_count': new_count,
            }
        except Exception as e:
            logger.error("Failed to regenerate candidate: %s", e)
            return None

    def create_candidate_row(self, candidate_data: Dict[str, Any]) -> Optional[str]:
        """Create new candidate row in Google Sheets.

        Returns:
            New candidate_id, or None if failed
        """
        try:
            self._ensure_candidates_tab()
            candidates = self.sheet.worksheet('Candidates')

            candidate_id = candidate_data.get('candidate_id', f"CAND-{uuid.uuid4().hex[:8].upper()}")

            new_values = {
                'candidate_id': candidate_id,
                'candidate_name': candidate_data.get('candidate_name', ''),
                'candidate_email': candidate_data.get('candidate_email', ''),
                'phone_number': candidate_data.get('phone_number', ''),
                'queue': candidate_data.get('queue', ''),
                'batch': candidate_data.get('batch', ''),
                'sub_batch': candidate_data.get('sub_batch', ''),
                'ta_email': candidate_data.get('ta_email', ''),
                'reschedule_count': 0,
                'test_call_status': 'pending',
                'regeneration_count': 0,
                'test_session_id': f"TEST-{uuid.uuid4().hex[:8].upper()}",
            }
            ordered_keys = sorted(CANDIDATE_COLS, key=lambda k: CANDIDATE_COLS[k])
            new_row = [new_values.get(k, '') for k in ordered_keys]

            candidates.append_row(new_row)
            logger.info("Created candidate row: %s", candidate_id)
            return candidate_id
        except Exception as e:
            logger.error("Failed to create candidate row: %s", e)
            return None

    # =========================================================================
    # Classifications CRUD
    # =========================================================================

    CLASSIFICATION_HEADERS = [
        'ClassificationID', 'Name', 'Description', 'CreatedAt', 'UpdatedAt', 'Status',
        'MinDurationMinutes', 'MaxDurationMinutes',
        'DefaultDifficulty', 'DefaultPersonality', 'DefaultKnowledgeable',
    ]

    def _ensure_classifications_tab(self):
        """Create Classifications tab if it doesn't exist. Also migrates an
        existing tab missing the newer per-classification default-settings
        columns, grid-width safe."""
        try:
            ws = self.sheet.worksheet('Classifications')
            existing = ws.row_values(1)
            missing = [h for h in self.CLASSIFICATION_HEADERS if h not in existing]
            if missing:
                logger.info("Migrating Classifications tab: adding %s", missing)
                start_col = len(existing) + 1
                if ws.col_count < start_col + len(missing) - 1:
                    ws.add_cols(start_col + len(missing) - 1 - ws.col_count)
                for i, header in enumerate(missing):
                    ws.update_cell(1, start_col + i, header)
        except gspread.exceptions.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title='Classifications', rows=100, cols=len(self.CLASSIFICATION_HEADERS))
            ws.append_row(self.CLASSIFICATION_HEADERS)
            logger.info("Created Classifications tab")

    @staticmethod
    def _classification_col(ws, header_name: str) -> int:
        """1-indexed column number for a Classifications tab header, read live
        (never hardcoded) so a migration appending columns at the end can't
        misalign a write."""
        headers = ws.row_values(1)
        return headers.index(header_name) + 1

    def get_classifications(self) -> list[Dict[str, Any]]:
        """Get all classifications."""
        try:
            self._ensure_classifications_tab()
            ws = self.sheet.worksheet('Classifications')
            records = ws.get_all_records()
            results = []
            for row in records:
                cid = str(row.get('ClassificationID', '')).strip()
                if not cid:
                    continue
                results.append({
                    'classification_id': cid,
                    'name': row.get('Name', ''),
                    'description': row.get('Description', ''),
                    'created_at': row.get('CreatedAt', ''),
                    'updated_at': row.get('UpdatedAt', ''),
                    'status': row.get('Status', 'active'),
                    'min_duration_minutes': row.get('MinDurationMinutes', ''),
                    'max_duration_minutes': row.get('MaxDurationMinutes', ''),
                    'default_difficulty': row.get('DefaultDifficulty', '') or 'random',
                    'default_personality': row.get('DefaultPersonality', '') or 'random',
                    'default_knowledgeable': row.get('DefaultKnowledgeable', '') or 'random',
                })
            return results
        except Exception as e:
            logger.error("Failed to get classifications: %s", e)
            return []

    def get_classification(self, classification_id: str) -> Optional[Dict[str, Any]]:
        """Get a single classification by ID."""
        for c in self.get_classifications():
            if c['classification_id'] == classification_id:
                return c
        return None

    def create_classification(
        self, name: str, description: str = "",
        min_duration_minutes: str = "", max_duration_minutes: str = "",
        default_difficulty: str = "random", default_personality: str = "random",
        default_knowledgeable: str = "random",
    ) -> Optional[Dict[str, Any]]:
        """Create a new classification. Returns the created classification."""
        try:
            self._ensure_classifications_tab()
            ws = self.sheet.worksheet('Classifications')
            classification_id = f"CL-{uuid.uuid4().hex[:8].upper()}"
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            values = {
                'ClassificationID': classification_id, 'Name': name, 'Description': description,
                'CreatedAt': now, 'UpdatedAt': now, 'Status': 'active',
                'MinDurationMinutes': min_duration_minutes, 'MaxDurationMinutes': max_duration_minutes,
                'DefaultDifficulty': default_difficulty or 'random',
                'DefaultPersonality': default_personality or 'random',
                'DefaultKnowledgeable': default_knowledgeable or 'random',
            }
            headers = ws.row_values(1)
            ws.append_row([values.get(h, '') for h in headers])
            logger.info("Created classification: %s (%s)", classification_id, name)
            return {
                'classification_id': classification_id,
                'name': name,
                'description': description,
                'created_at': now,
                'updated_at': now,
                'status': 'active',
                'min_duration_minutes': min_duration_minutes,
                'max_duration_minutes': max_duration_minutes,
                'default_difficulty': values['DefaultDifficulty'],
                'default_personality': values['DefaultPersonality'],
                'default_knowledgeable': values['DefaultKnowledgeable'],
            }
        except Exception as e:
            logger.error("Failed to create classification: %s", e)
            return None

    def update_classification(
        self, classification_id: str, name: str = None, description: str = None,
        min_duration_minutes: str = None, max_duration_minutes: str = None,
        default_difficulty: str = None, default_personality: str = None,
        default_knowledgeable: str = None,
    ) -> bool:
        """Update an existing classification. Returns True if found and updated."""
        try:
            self._ensure_classifications_tab()
            ws = self.sheet.worksheet('Classifications')
            records = ws.get_all_records()
            from datetime import datetime, timezone
            field_map = {
                'Name': name, 'Description': description,
                'MinDurationMinutes': min_duration_minutes, 'MaxDurationMinutes': max_duration_minutes,
                'DefaultDifficulty': default_difficulty, 'DefaultPersonality': default_personality,
                'DefaultKnowledgeable': default_knowledgeable,
            }
            for i, row in enumerate(records, start=2):
                if str(row.get('ClassificationID', '')).strip() == classification_id:
                    for header, value in field_map.items():
                        if value is not None:
                            ws.update_cell(i, self._classification_col(ws, header), value)
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    ws.update_cell(i, self._classification_col(ws, 'UpdatedAt'), now)
                    logger.info("Updated classification: %s", classification_id)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to update classification: %s", e)
            return False

    def reset_ai_clients_to_classification_defaults(self, classification_id: str) -> int:
        """Bulk-overwrite every AI Client in this classification's Difficulty,
        Personality and Knowledgeable back to the classification's own current
        default settings. Returns the number of clients updated."""
        classification = self.get_classification(classification_id)
        if not classification:
            return 0
        try:
            self._ensure_ai_clients_tab()
            ws = self.sheet.worksheet('AI Clients')
            records = ws.get_all_records()
            headers = ws.row_values(1)
            difficulty_col = headers.index('Difficulty') + 1
            personality_col = headers.index('Personality') + 1
            knowledgeable_col = headers.index('Knowledgeable') + 1
            count = 0
            for i, row in enumerate(records, start=2):
                if str(row.get('ClassificationID', '')).strip() != classification_id:
                    continue
                ws.update_cell(i, difficulty_col, classification['default_difficulty'])
                ws.update_cell(i, personality_col, classification['default_personality'])
                ws.update_cell(i, knowledgeable_col, classification['default_knowledgeable'])
                count += 1
            logger.info("Reset %d AI client(s) in classification %s to defaults", count, classification_id)
            return count
        except Exception as e:
            logger.error("Failed to reset AI clients for classification %s: %s", classification_id, e)
            return 0

    def delete_classification(self, classification_id: str) -> bool:
        """Delete a classification. Returns True if found and deleted."""
        try:
            self._ensure_classifications_tab()
            ws = self.sheet.worksheet('Classifications')
            records = ws.get_all_records()
            for i, row in enumerate(records, start=2):
                if str(row.get('ClassificationID', '')).strip() == classification_id:
                    ws.delete_rows(i, i)
                    logger.info("Deleted classification: %s", classification_id)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to delete classification: %s", e)
            return False

    # =========================================================================
    # Test Calls Checklist (live, per-Classification evaluation configuration)
    # =========================================================================

    TEST_CALLS_CHECKLIST_HEADERS = [
        'Classification', 'Criterion', 'Weight', 'Enabled', 'Description',
        'Evaluation Instructions', 'Version', 'Last Updated',
    ]

    # One-time seed for the "Test Calls" classification, written only when the
    # tab is first created. After that, Google Sheets is the sole source of
    # truth — this constant is never read again at evaluation time.
    _DEFAULT_TEST_CALLS_CHECKLIST = [
        ('Product Knowledge', 15, 'Accuracy of Daftra product knowledge',
         "Evaluate whether the salesperson demonstrates accurate knowledge of Daftra's products, "
         "features, capabilities, and relevant use cases. Do not reward invented information."),
        ('Listening + Discovery', 15, "Ability to discover customer needs",
         "Evaluate whether the salesperson asks appropriate questions and actually listens to the "
         "customer's answers to discover the business situation."),
        ('Pricing Information & Knowledge', 12, 'Accuracy and handling of pricing questions',
         "Evaluate how accurately and appropriately the salesperson handles pricing-related "
         "questions. Dynamic pricing information must not be invented."),
        ('Tone of Voice', 8, 'Professional and appropriate communication',
         "Evaluate professionalism, confidence, respect, friendliness, and suitability of communication."),
        ('Call Structure & Flow', 10, 'Logical progression of the call',
         "Evaluate whether the conversation progresses logically from introduction/discovery "
         "through solution discussion, objections, and next steps."),
        ('Clarity & Simplicity', 10, 'Clear and easy-to-understand explanations',
         "Evaluate whether explanations are understandable, concise, and appropriate for the "
         "customer's level of knowledge."),
        ('Identifying Customer Needs & Fitting Customer Needs to Our System', 10,
         'Ability to connect needs with Daftra capabilities',
         "Evaluate whether the salesperson understands the customer's actual needs and connects "
         "them appropriately to Daftra capabilities."),
        ('Negotiation Skills & Handling Objections', 10, 'Objection handling and negotiation',
         "Evaluate how effectively the salesperson responds to objections, concerns, hesitation, "
         "price concerns, competitors, and other barriers."),
        ('Closing the Deal / Follow-up', 10, 'Closing and appropriate next steps',
         "Evaluate whether the salesperson identifies an appropriate next step and moves the "
         "customer toward a meaningful continuation of the sales process."),
    ]

    def _ensure_test_calls_checklist_tab(self):
        """Create the 'Test Calls Checklist' tab if it doesn't exist, seeded with the
        current default checklist for the 'Test Calls' classification. Also migrates
        old sheets by appending any missing columns (grid-width safe)."""
        try:
            ws = self.sheet.worksheet('Test Calls Checklist')
            existing = ws.row_values(1)
            missing = [h for h in self.TEST_CALLS_CHECKLIST_HEADERS if h not in existing]
            if missing:
                logger.info("Migrating Test Calls Checklist tab: adding %s", missing)
                start_col = len(existing) + 1
                if ws.col_count < start_col + len(missing) - 1:
                    ws.add_cols(start_col + len(missing) - 1 - ws.col_count)
                for i, header in enumerate(missing):
                    ws.update_cell(1, start_col + i, header)
        except gspread.exceptions.WorksheetNotFound:
            ws = self.sheet.add_worksheet(
                title='Test Calls Checklist', rows=100, cols=len(self.TEST_CALLS_CHECKLIST_HEADERS)
            )
            ws.append_row(self.TEST_CALLS_CHECKLIST_HEADERS)
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            for criterion, weight, description, instructions in self._DEFAULT_TEST_CALLS_CHECKLIST:
                ws.append_row(['Test Calls', criterion, weight, 'TRUE', description, instructions, '1', now])
            logger.info("Created Test Calls Checklist tab with default 'Test Calls' criteria")

    def get_checklist(self, classification_name: str) -> Optional[list[Dict[str, Any]]]:
        """Get ALL checklist rows (enabled and disabled) for an exact classification name,
        read fresh from Google Sheets every call (no caching — this is what makes the
        checklist "live"). Returns [] if the classification genuinely has no rows,
        or None if the Sheets read itself failed (so callers can tell "not configured"
        apart from "temporary Sheets error" and report each distinctly).
        Weight is returned as-is (string/number) for validate_checklist() to check.
        """
        try:
            self._ensure_test_calls_checklist_tab()
            ws = self.sheet.worksheet('Test Calls Checklist')
            records = ws.get_all_records()
            results = []
            for row in records:
                name = str(row.get('Classification', '')).strip()
                if name != classification_name:
                    continue
                criterion = str(row.get('Criterion', '')).strip()
                if not criterion:
                    continue
                results.append({
                    'classification': name,
                    'criterion': criterion,
                    'weight': row.get('Weight', ''),
                    'enabled': row.get('Enabled', ''),
                    'description': row.get('Description', ''),
                    'instructions': row.get('Evaluation Instructions', ''),
                    'version': row.get('Version', ''),
                    'last_updated': row.get('Last Updated', ''),
                })
            return results
        except Exception as e:
            logger.error("Failed to get checklist for classification %r: %s", classification_name, e)
            return None

    # =========================================================================
    # AI Clients CRUD
    # =========================================================================

    AI_CLIENT_HEADERS = [
        'ClientID', 'Name', 'Country', 'Queue', 'CustomerLanguage', 'ClassificationID', 'Scenario', 'Dialect',
        'Difficulty', 'Personality', 'Instructions', 'Objectives', 'Objections',
        'CustomerName', 'CustomerRole', 'CompanyName', 'BusinessField',
        'CompanySize', 'PainPoint', 'DecisionMaker', 'BudgetSensitivity',
        'BuyingIntent', 'Temperature', 'ProductBrief', 'Voice',
        'Knowledgeable', 'Active', 'CreatedAt',
    ]

    # Country is the single source of truth for dialect — a client's Country
    # always determines its Dialect (never set independently), so the random
    # voice-selection logic can never mismatch a Saudi client with an Egyptian
    # voice or vice versa.
    COUNTRY_TO_DIALECT = {'sa': 'saudi', 'eg': 'egyptian'}

    # The sales-role queue this AI Client's calls are routed under — see the
    # detailed meaning of each in the AI Client form; purely descriptive/filtering,
    # doesn't drive any other field.
    QUEUE_OPTIONS = ['Global SDR', 'EG SDR', 'Global Sales', 'EG Sales', 'KSA SDR', 'KSA Sales']

    def _ensure_ai_clients_tab(self):
        """Create AI Clients tab if it doesn't exist. Also migrate old headers."""
        try:
            ws = self.sheet.worksheet('AI Clients')
            # Check if headers need migration
            existing = ws.row_values(1)
            if 'CustomerLanguage' not in existing:
                logger.info("Migrating AI Clients tab: adding CustomerLanguage column")
                # Insert CustomerLanguage after Name (column C = index 2)
                ws.insert_columns(3, 1)
                ws.update_cell(1, 3, 'CustomerLanguage')
                # Set default value for existing rows
                num_rows = len(ws.get_all_values())
                for r in range(2, num_rows + 1):
                    ws.update_cell(r, 3, 'ar')
                logger.info("Migration complete: added CustomerLanguage to %d rows", num_rows - 1)
                existing = ws.row_values(1)
            if 'Queue' not in existing:
                logger.info("Migrating AI Clients tab: adding Queue column")
                # Queue sits right after Country (column C = index 3)
                ws.insert_columns(4, 1)
                ws.update_cell(1, 4, 'Queue')
                logger.info("Migration complete: added Queue column")
        except gspread.exceptions.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title='AI Clients', rows=100, cols=len(self.AI_CLIENT_HEADERS))
            ws.append_row(self.AI_CLIENT_HEADERS)
            logger.info("Created AI Clients tab")

    def _parse_ai_client_row(self, row: dict) -> Optional[Dict[str, Any]]:
        """Parse an AI Client row into a dict."""
        cid = str(row.get('ClientID', '')).strip()
        if not cid:
            return None
        return {
            'client_id': cid,
            'name': row.get('Name', ''),
            'classification_id': row.get('ClassificationID', ''),
            'queue': row.get('Queue', ''),
            'scenario': row.get('Scenario', 'new-lead-discovery-call'),
            'dialect': row.get('Dialect', 'saudi'),
            'customer_language': row.get('CustomerLanguage', 'ar'),
            'difficulty': row.get('Difficulty', 'medium'),
            'personality': row.get('Personality', ''),
            'instructions': row.get('Instructions', ''),
            'objectives': row.get('Objectives', ''),
            'objections': row.get('Objections', ''),
            'customer_name': row.get('CustomerName', ''),
            'customer_role': row.get('CustomerRole', ''),
            'company_name': row.get('CompanyName', ''),
            'business_field': row.get('BusinessField', ''),
            'company_size': row.get('CompanySize', ''),
            'pain_point': row.get('PainPoint', ''),
            'decision_maker': row.get('DecisionMaker', 'true').lower() == 'true',
            'budget_sensitivity': row.get('BudgetSensitivity', 'متوسطة'),
            'buying_intent': row.get('BuyingIntent', 'متوسطة'),
            'temperature': row.get('Temperature', 'warm'),
            'product_brief': row.get('ProductBrief', ''),
            'voice': row.get('Voice', ''),
            # Tristate: 'true' / 'false' / 'random' (resolved per-call in agent.py,
            # same pattern as difficulty/personality) — kept as the raw string here.
            'knowledgeable': str(row.get('Knowledgeable', 'false') or 'false'),
            'active': row.get('Active', 'true').lower() == 'true',
            'created_at': row.get('CreatedAt', ''),
            'country': row.get('Country', '') or ('sa' if row.get('Dialect', '') == 'saudi' else 'eg'),
        }

    def get_ai_clients(self, classification_id: str = None, active_only: bool = False) -> list[Dict[str, Any]]:
        """Get AI clients, optionally filtered by classification and/or active status."""
        try:
            self._ensure_ai_clients_tab()
            ws = self.sheet.worksheet('AI Clients')
            records = ws.get_all_records()
            results = []
            for row in records:
                client = self._parse_ai_client_row(row)
                if not client:
                    continue
                if classification_id and client['classification_id'] != classification_id:
                    continue
                if active_only and not client['active']:
                    continue
                results.append(client)
            return results
        except Exception as e:
            logger.error("Failed to get AI clients: %s", e)
            return []

    def get_ai_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        """Get a single AI client by ID."""
        for c in self.get_ai_clients():
            if c['client_id'] == client_id:
                return c
        return None

    def create_ai_client(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Create a new AI client. Returns the created client.

        If difficulty/personality/knowledgeable aren't explicitly given, they
        default from the parent Classification's own default settings (falling
        back to "random"/"medium" if there's no classification or no defaults set)."""
        try:
            self._ensure_ai_clients_tab()
            ws = self.sheet.worksheet('AI Clients')
            client_id = f"AC-{uuid.uuid4().hex[:8].upper()}"
            from datetime import datetime, timezone
            created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            country = data.get('country', '') or ('sa' if data.get('dialect') == 'saudi' else 'eg')
            dialect = self.COUNTRY_TO_DIALECT.get(country, 'saudi')

            classification_id = data.get('classification_id', '')
            classification = self.get_classification(classification_id) if classification_id else None
            default_difficulty = (classification or {}).get('default_difficulty', 'medium')
            default_personality = (classification or {}).get('default_personality', 'random')
            default_knowledgeable = (classification or {}).get('default_knowledgeable', 'random')

            row = [
                client_id,
                data.get('name', ''),
                country,
                data.get('queue', ''),
                data.get('customer_language', 'ar'),
                classification_id,
                data.get('scenario', 'new-lead-discovery-call'),
                dialect,
                data.get('difficulty') or default_difficulty,
                data.get('personality') if data.get('personality') is not None else default_personality,
                data.get('instructions', ''),
                data.get('objectives', ''),
                data.get('objections', ''),
                data.get('customer_name', ''),
                data.get('customer_role', ''),
                data.get('company_name', ''),
                data.get('business_field', ''),
                data.get('company_size', ''),
                data.get('pain_point', ''),
                str(data.get('decision_maker', True)),
                data.get('budget_sensitivity', 'متوسطة'),
                data.get('buying_intent', 'متوسطة'),
                data.get('temperature', 'warm'),
                data.get('product_brief', ''),
                data.get('voice', ''),
                str(data.get('knowledgeable')) if data.get('knowledgeable') is not None else default_knowledgeable,
                str(data.get('active', True)),
                created_at,
            ]
            ws.append_row(row)
            logger.info("Created AI client: %s (%s)", client_id, data.get('name', ''))
            return self.get_ai_client(client_id)
        except Exception as e:
            logger.error("Failed to create AI client: %s", e)
            return None

    def update_ai_client(self, client_id: str, data: Dict[str, Any]) -> bool:
        """Update an existing AI client. Returns True if found and updated."""
        try:
            self._ensure_ai_clients_tab()
            ws = self.sheet.worksheet('AI Clients')
            records = ws.get_all_records()
            for i, row in enumerate(records, start=2):
                if str(row.get('ClientID', '')).strip() == client_id:
                    # 'dialect' is intentionally NOT in field_map — it is never set
                    # independently, only derived from 'country' below, so a client's
                    # dialect (and therefore its voice pool) can never drift out of
                    # sync with its country.
                    field_map = {
                        'name': 2, 'country': 3, 'queue': 4, 'customer_language': 5,
                        'classification_id': 6, 'scenario': 7,
                        'difficulty': 9, 'personality': 10,
                        'instructions': 11, 'objectives': 12, 'objections': 13,
                        'customer_name': 14, 'customer_role': 15, 'company_name': 16,
                        'business_field': 17, 'company_size': 18, 'pain_point': 19,
                        'decision_maker': 20, 'budget_sensitivity': 21,
                        'buying_intent': 22, 'temperature': 23, 'product_brief': 24,
                        'voice': 25, 'knowledgeable': 26,
                        'active': 27,
                    }
                    for key, val in data.items():
                        if key in field_map and val is not None:
                            ws.update_cell(i, field_map[key], str(val))
                    if data.get('country') is not None:
                        dialect = self.COUNTRY_TO_DIALECT.get(data['country'], 'saudi')
                        ws.update_cell(i, 8, dialect)
                    logger.info("Updated AI client: %s", client_id)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to update AI client: %s", e)
            return False

    def delete_ai_client(self, client_id: str) -> bool:
        """Delete an AI client. Returns True if found and deleted."""
        try:
            self._ensure_ai_clients_tab()
            ws = self.sheet.worksheet('AI Clients')
            records = ws.get_all_records()
            for i, row in enumerate(records, start=2):
                if str(row.get('ClientID', '')).strip() == client_id:
                    ws.delete_rows(i, i)
                    logger.info("Deleted AI client: %s", client_id)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to delete AI client: %s", e)
            return False

    # =========================================================================
    # Attempts (One-time Test Call Links)
    # =========================================================================

    ATTEMPT_HEADERS = [
        'AttemptID', 'AccessToken', 'CandidateID', 'CandidateEmail', 'CandidateName',
        'ClassificationID', 'AIClientID', 'Status', 'Score', 'CreatedAt', 'UsedAt',
    ]

    # =========================================================================
    # Link Settings (generic key/value config for the test-call link system)
    # =========================================================================

    LINK_SETTINGS_HEADERS = ['Key', 'Value']
    DEFAULT_MAX_RESCHEDULE_COUNT = 1

    def _ensure_link_settings_tab(self):
        """Create the "Link Settings" tab if it doesn't exist — a small generic
        key/value store for admin-configurable settings related to test-call
        links (currently just MaxRescheduleCount, extensible for more later)."""
        try:
            self.sheet.worksheet('Link Settings')
        except gspread.exceptions.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title='Link Settings', rows=20, cols=len(self.LINK_SETTINGS_HEADERS))
            ws.append_row(self.LINK_SETTINGS_HEADERS)
            logger.info("Created Link Settings tab")

    def get_link_setting(self, key: str, default: str = '') -> str:
        """Read a single key/value setting from the Link Settings tab."""
        try:
            self._ensure_link_settings_tab()
            ws = self.sheet.worksheet('Link Settings')
            for row in ws.get_all_records():
                if str(row.get('Key', '')).strip() == key:
                    val = str(row.get('Value', '')).strip()
                    return val if val else default
            return default
        except Exception as e:
            logger.error("Failed to read link setting %r: %s", key, e)
            return default

    def set_link_setting(self, key: str, value: str) -> bool:
        """Create or update a single key/value setting in the Link Settings tab."""
        try:
            self._ensure_link_settings_tab()
            ws = self.sheet.worksheet('Link Settings')
            records = ws.get_all_records()
            for i, row in enumerate(records, start=2):
                if str(row.get('Key', '')).strip() == key:
                    ws.update_cell(i, 2, value)
                    return True
            ws.append_row([key, value])
            return True
        except Exception as e:
            logger.error("Failed to set link setting %r: %s", key, e)
            return False

    def get_max_reschedule_count(self) -> int:
        """Max number of times a candidate may reschedule and still get an
        auto-generated link. -1 means unlimited."""
        raw = self.get_link_setting('MaxRescheduleCount', str(self.DEFAULT_MAX_RESCHEDULE_COUNT))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return self.DEFAULT_MAX_RESCHEDULE_COUNT

    def _ensure_attempts_tab(self):
        """Create the "Generate Link" tab if it doesn't exist — this is the manual,
        admin-triggered link flow (from inside the AI Simulator platform), kept
        separate from the auto-generated links written directly onto a real
        candidate's own row in the Candidates sheet. Also migrates an existing
        tab missing newer columns (e.g. "Score"), grid-width safe."""
        try:
            ws = self.sheet.worksheet('Generate Link')
            existing = ws.row_values(1)
            missing = [h for h in self.ATTEMPT_HEADERS if h not in existing]
            if missing:
                logger.info("Migrating Generate Link tab: adding %s", missing)
                start_col = len(existing) + 1
                if ws.col_count < start_col + len(missing) - 1:
                    ws.add_cols(start_col + len(missing) - 1 - ws.col_count)
                for i, header in enumerate(missing):
                    ws.update_cell(1, start_col + i, header)
        except gspread.exceptions.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title='Generate Link', rows=100, cols=len(self.ATTEMPT_HEADERS))
            ws.append_row(self.ATTEMPT_HEADERS)
            logger.info("Created Generate Link tab")

    @staticmethod
    def _attempt_col(ws, header_name: str) -> int:
        """1-indexed column number for a "Generate Link" tab header, read live from
        the sheet's actual header row (never hardcoded) — so a migration that
        appends a missing column at the end can never misalign a write."""
        headers = ws.row_values(1)
        return headers.index(header_name) + 1

    def create_attempt(self, candidate_id: str, candidate_email: str, candidate_name: str,
                       classification_id: str = "", ai_client_id: str = "") -> Optional[Dict[str, Any]]:
        """Create a one-time test call attempt link. Returns attempt data with secure token."""
        try:
            self._ensure_attempts_tab()
            ws = self.sheet.worksheet('Generate Link')
            attempt_id = f"ATT-{uuid.uuid4().hex[:12].upper()}"
            access_token = secrets.token_urlsafe(32)
            from datetime import datetime, timezone
            created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            values = {
                'AttemptID': attempt_id, 'AccessToken': access_token, 'CandidateID': candidate_id,
                'CandidateEmail': candidate_email, 'CandidateName': candidate_name,
                'ClassificationID': classification_id, 'AIClientID': ai_client_id,
                'Status': 'pending', 'Score': '', 'CreatedAt': created_at, 'UsedAt': '',
            }
            headers = ws.row_values(1)
            ws.append_row([values.get(h, '') for h in headers])
            logger.info("Created attempt: %s for candidate %s", attempt_id, candidate_id)
            return {
                'attempt_id': attempt_id,
                'access_token': access_token,
                'candidate_id': candidate_id,
                'candidate_email': candidate_email,
                'candidate_name': candidate_name,
                'classification_id': classification_id,
                'ai_client_id': ai_client_id,
                'status': 'pending',
                'created_at': created_at,
            }
        except Exception as e:
            logger.error("Failed to create attempt: %s", e)
            return None

    def get_attempt(self, attempt_id: str) -> Optional[Dict[str, Any]]:
        """Get an attempt by ID."""
        try:
            self._ensure_attempts_tab()
            ws = self.sheet.worksheet('Generate Link')
            records = ws.get_all_records()
            for row in records:
                if str(row.get('AttemptID', '')).strip() == attempt_id:
                    return {
                        'attempt_id': row.get('AttemptID', ''),
                        'access_token': row.get('AccessToken', ''),
                        'candidate_id': row.get('CandidateID', ''),
                        'candidate_email': row.get('CandidateEmail', ''),
                        'candidate_name': row.get('CandidateName', ''),
                        'classification_id': row.get('ClassificationID', ''),
                        'ai_client_id': row.get('AIClientID', ''),
                        'status': row.get('Status', 'pending'),
                        'created_at': row.get('CreatedAt', ''),
                        'used_at': row.get('UsedAt', ''),
                    }
            return None
        except Exception as e:
            logger.error("Failed to get attempt: %s", e)
            return None

    def get_attempt_by_token(self, access_token: str) -> Optional[Dict[str, Any]]:
        """Get an attempt by its secure access token."""
        try:
            self._ensure_attempts_tab()
            ws = self.sheet.worksheet('Generate Link')
            records = ws.get_all_records()
            for row in records:
                if str(row.get('AccessToken', '')).strip() == access_token:
                    return {
                        'attempt_id': row.get('AttemptID', ''),
                        'access_token': row.get('AccessToken', ''),
                        'candidate_id': row.get('CandidateID', ''),
                        'candidate_email': row.get('CandidateEmail', ''),
                        'candidate_name': row.get('CandidateName', ''),
                        'classification_id': row.get('ClassificationID', ''),
                        'ai_client_id': row.get('AIClientID', ''),
                        'status': row.get('Status', 'pending'),
                        'created_at': row.get('CreatedAt', ''),
                        'used_at': row.get('UsedAt', ''),
                    }
            return None
        except Exception as e:
            logger.error("Failed to get attempt by token: %s", e)
            return None

    def consume_attempt(self, attempt_id: str) -> bool:
        """Mark an attempt as used (one-time consumption). Returns True if successful."""
        try:
            self._ensure_attempts_tab()
            ws = self.sheet.worksheet('Generate Link')
            records = ws.get_all_records()
            from datetime import datetime, timezone
            for i, row in enumerate(records, start=2):
                if str(row.get('AttemptID', '')).strip() == attempt_id:
                    status = str(row.get('Status', '')).strip()
                    if status != 'pending':
                        return False  # Already used or expired
                    used_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    ws.update_cell(i, self._attempt_col(ws, 'Status'), 'used')
                    ws.update_cell(i, self._attempt_col(ws, 'UsedAt'), used_at)
                    logger.info("Consumed attempt: %s", attempt_id)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to consume attempt: %s", e)
            return False

    def get_attempts_for_candidate(self, candidate_id: str) -> list[Dict[str, Any]]:
        """Get all attempts for a candidate."""
        try:
            self._ensure_attempts_tab()
            ws = self.sheet.worksheet('Generate Link')
            records = ws.get_all_records()
            results = []
            for row in records:
                if str(row.get('CandidateID', '')).strip() == candidate_id:
                    results.append({
                        'attempt_id': row.get('AttemptID', ''),
                        'status': row.get('Status', 'pending'),
                        'created_at': row.get('CreatedAt', ''),
                        'used_at': row.get('UsedAt', ''),
                    })
            return results
        except Exception as e:
            logger.error("Failed to get attempts for candidate: %s", e)
            return []

    def get_attempts(self, classification_id: str = None) -> list[Dict[str, Any]]:
        """Get all attempts, optionally filtered by classification_id."""
        try:
            self._ensure_attempts_tab()
            ws = self.sheet.worksheet('Generate Link')
            records = ws.get_all_records()
            results = []
            for row in records:
                attempt = {
                    'attempt_id': row.get('AttemptID', ''),
                    'access_token': row.get('AccessToken', ''),
                    'candidate_id': row.get('CandidateID', ''),
                    'candidate_email': row.get('CandidateEmail', ''),
                    'candidate_name': row.get('CandidateName', ''),
                    'classification_id': row.get('ClassificationID', ''),
                    'ai_client_id': row.get('AIClientID', ''),
                    'status': row.get('Status', 'pending'),
                    'created_at': row.get('CreatedAt', ''),
                    'used_at': row.get('UsedAt', ''),
                }
                if classification_id and attempt['classification_id'] != classification_id:
                    continue
                results.append(attempt)
            return results
        except Exception as e:
            logger.error("Failed to get attempts: %s", e)
            return []

    # =========================================================================
    # Calls (Track AI Client Calls)
    # =========================================================================

    CALLS_HEADERS = [
        'CallID', 'Room', 'AIClientID', 'AIClientName', 'ClassificationID',
        'CallerName', 'StartedAt', 'EndedAt', 'Duration', 'Score', 'Status',
    ]

    def _ensure_calls_tab(self):
        """Create Calls tab if it doesn't exist."""
        try:
            self.sheet.worksheet('Calls')
        except gspread.exceptions.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title='Calls', rows=100, cols=len(self.CALLS_HEADERS))
            ws.append_row(self.CALLS_HEADERS)
            logger.info("Created Calls tab")

    def log_call(self, room: str, ai_client_id: str, ai_client_name: str,
                 classification_id: str, caller_name: str, call_id: str = "") -> Optional[Dict[str, Any]]:
        """Log a new call to the Calls sheet. Returns the call data.
        Pass call_id when the caller already generated one (e.g. to embed it in the
        recording folder name too) so the sheet and the files on disk stay in sync."""
        try:
            self._ensure_calls_tab()
            ws = self.sheet.worksheet('Calls')
            call_id = call_id or f"CALL-{uuid.uuid4().hex[:10].upper()}"
            from datetime import datetime
            started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ws.append_row([
                call_id, room, ai_client_id, ai_client_name, classification_id,
                caller_name, started_at, '', '', '', 'started',
            ])
            logger.info("Logged call: %s (room=%s)", call_id, room)
            return {
                'call_id': call_id,
                'room': room,
                'ai_client_id': ai_client_id,
                'ai_client_name': ai_client_name,
                'classification_id': classification_id,
                'caller_name': caller_name,
                'started_at': started_at,
                'status': 'started',
            }
        except Exception as e:
            logger.error("Failed to log call: %s", e)
            return None

    def update_call_end(self, room: str, score: float = 0) -> bool:
        """Update call with end time, duration, and score."""
        try:
            self._ensure_calls_tab()
            ws = self.sheet.worksheet('Calls')
            records = ws.get_all_records()
            from datetime import datetime
            for i, row in enumerate(records, start=2):
                if str(row.get('Room', '')).strip() == room:
                    ended_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    started_str = str(row.get('StartedAt', '')).strip()
                    duration = ''
                    if started_str:
                        try:
                            started_dt = datetime.strptime(started_str, "%Y-%m-%d %H:%M:%S")
                            ended_dt = datetime.strptime(ended_at, "%Y-%m-%d %H:%M:%S")
                            duration = str(int((ended_dt - started_dt).total_seconds()))
                        except Exception:
                            pass
                    ws.update_cell(i, 8, ended_at)   # EndedAt
                    ws.update_cell(i, 9, duration)    # Duration
                    ws.update_cell(i, 10, str(round(score, 1)) if score else '')  # Score
                    ws.update_cell(i, 11, 'completed')  # Status
                    logger.info("Updated call end: room=%s, score=%s", room, score)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to update call end: %s", e)
            return False

    def close_call_if_open(self, room: str, status: str) -> bool:
        """End a call that is still 'started' (never got a result) with `status`
        ('failed' / 'interrupted'). Leaves completed calls alone. Returns True
        if a row was closed."""
        try:
            self._ensure_calls_tab()
            ws = self.sheet.worksheet('Calls')
            records = ws.get_all_records()
            for i, row in enumerate(records, start=2):
                if str(row.get('Room', '')).strip() == room:
                    if str(row.get('Status', '')).strip() != 'started':
                        return False
                    self._close_call_row(ws, i, row, status)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to close call %s: %s", room, e)
            return False

    def close_stale_calls(self, max_minutes_by_classification: Dict[str, float],
                          default_max_minutes: float, grace_minutes: float) -> list[Dict[str, Any]]:
        """Mark 'started' calls older than their classification's max duration
        (or `default_max_minutes`) + `grace_minutes` as 'interrupted' — the agent
        died before it could report. Returns the calls it closed."""
        from datetime import datetime, timedelta
        closed = []
        try:
            self._ensure_calls_tab()
            ws = self.sheet.worksheet('Calls')
            now = datetime.now()
            for i, row in enumerate(ws.get_all_records(), start=2):
                if str(row.get('Status', '')).strip() != 'started':
                    continue
                try:
                    started = datetime.strptime(str(row.get('StartedAt', '')).strip(), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                limit = max_minutes_by_classification.get(
                    str(row.get('ClassificationID', '')).strip()) or default_max_minutes
                if now - started > timedelta(minutes=limit + grace_minutes):
                    self._close_call_row(ws, i, row, 'interrupted')
                    closed.append({'call_id': row.get('CallID', ''), 'room': row.get('Room', ''),
                                   'started_at': row.get('StartedAt', '')})
        except Exception as e:
            logger.error("Failed to close stale calls: %s", e)
        return closed

    @staticmethod
    def _close_call_row(ws, i: int, row: Dict[str, Any], status: str) -> None:
        from datetime import datetime
        ended_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            started = datetime.strptime(str(row.get('StartedAt', '')).strip(), "%Y-%m-%d %H:%M:%S")
            duration = str(int((datetime.now() - started).total_seconds()))
        except ValueError:
            duration = ''
        ws.update(values=[[ended_at, duration]], range_name=f"H{i}:I{i}")  # EndedAt, Duration
        ws.update_cell(i, 11, status)                     # Status
        logger.info("Closed call row %d (room=%s) as %s", i, row.get('Room', ''), status)

    def get_calls(self, classification_id: str = None) -> list[Dict[str, Any]]:
        """Get all calls, optionally filtered by classification."""
        try:
            self._ensure_calls_tab()
            ws = self.sheet.worksheet('Calls')
            records = ws.get_all_records()
            results = []
            for row in records:
                call = {
                    'call_id': row.get('CallID', ''),
                    'room': row.get('Room', ''),
                    'ai_client_id': row.get('AIClientID', ''),
                    'ai_client_name': row.get('AIClientName', ''),
                    'classification_id': row.get('ClassificationID', ''),
                    'caller_name': row.get('CallerName', ''),
                    'started_at': row.get('StartedAt', ''),
                    'ended_at': row.get('EndedAt', ''),
                    'duration': row.get('Duration', ''),
                    'score': row.get('Score', ''),
                    'status': row.get('Status', ''),
                }
                if classification_id and call['classification_id'] != classification_id:
                    continue
                results.append(call)
            return results
        except Exception as e:
            logger.error("Failed to get calls: %s", e)
            return []

    def delete_call(self, room: str) -> bool:
        """Delete a call row from the Calls sheet by room. Returns True if found and deleted."""
        try:
            self._ensure_calls_tab()
            ws = self.sheet.worksheet('Calls')
            records = ws.get_all_records()
            for i, row in enumerate(records, start=2):
                if str(row.get('Room', '')).strip() == room:
                    ws.delete_rows(i, i)
                    logger.info("Deleted call: room=%s", room)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to delete call: %s", e)
            return False

    def update_attempt_score(self, attempt_id: str, score: float) -> bool:
        """Update attempt with evaluation score."""
        try:
            self._ensure_attempts_tab()
            ws = self.sheet.worksheet('Generate Link')
            records = ws.get_all_records()
            for i, row in enumerate(records, start=2):
                if str(row.get('AttemptID', '')).strip() == attempt_id:
                    ws.update_cell(i, self._attempt_col(ws, 'Score'), str(round(score, 1)))
                    logger.info("Updated attempt score: %s = %s", attempt_id, score)
                    return True
            return False
        except Exception as e:
            logger.error("Failed to update attempt score: %s", e)
            return False


# Singleton client
_sheets_client: Optional[GoogleSheetsClient] = None


def get_sheets_client() -> GoogleSheetsClient:
    """Get or create Google Sheets client singleton."""
    global _sheets_client
    if _sheets_client is None:
        _sheets_client = GoogleSheetsClient()
    return _sheets_client
