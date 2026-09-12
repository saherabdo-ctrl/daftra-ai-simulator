"""Google Sheets client for HiringFlow AI Simulator.

Reads from Heads sheet (internal users) and Candidates sheet (test call candidates).
Google Sheets is the single source of truth.
"""

import os
import uuid
import logging
from typing import Optional, Dict, Any

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger("sheets")

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.readonly',
]

# Column indices for Candidates sheet (1-indexed)
# Actual columns from HiringFlow Google Sheet:
# A=CreatedAt, B=CandidateID, C=CandidateName, D=CandidateEmail,
# E=PhoneNumber, F=CVUrl, G=Queue, H=Batch, I=SubBatch, J=TAEmail,
# K=AcceptanceEmailScheduledAt, L=AcceptanceEmailSentAt, M=AcceptanceEmailStatus,
# N=BookingWindowStart, O=BookingWindowEnd, P=TestCallScheduledAt,
# Q=RescheduleCount, R=TestCallStatus, S=TestCallLinkSentAt,
# T=TestCallStartedAt, U=TestCallEndedAt, V=TestCallScore,
# W=TestCallResult, X=TestCallEvaluation, Y=DisputeStatus,
# Z=DisputeDecisionNotes, AA=HRTAApproval, AB=SalesApproval,
# AC=QualityApproval, AD=TATeamLeaderApproval, AE=HeadOfSalesApproval,
# AF=SendToOfferAt, AG=OfferSentAt, AH=OfferStatus, AI=OfferDecision,
# AJ=FinalCandidateStatus, AK=PreviousTestCallScore, AL=PreviousTestCallResult,
# AM=PreviousTestCallEvaluation, AN=RegenerationCount, AO=TestSessionId
CANDIDATE_COLS = {
    'created_at': 1,           # A
    'candidate_id': 2,         # B
    'candidate_name': 3,       # C
    'candidate_email': 4,      # D
    'phone_number': 5,         # E
    'cv_url': 6,               # F
    'queue': 7,                # G
    'batch': 8,                # H
    'sub_batch': 9,            # I
    'ta_email': 10,            # J
    'acceptance_email_scheduled_at': 11,  # K
    'acceptance_email_sent_at': 12,       # L
    'acceptance_email_status': 13,        # M
    'booking_window_start': 14,           # N
    'booking_window_end': 15,             # O
    'test_call_scheduled_at': 16,         # P
    'reschedule_count': 17,               # Q
    'test_call_status': 18,               # R
    'test_call_link_sent_at': 19,         # S
    'test_call_started_at': 20,           # T
    'test_call_ended_at': 21,             # U
    'test_call_score': 22,                # V
    'test_call_result': 23,               # W
    'test_call_evaluation': 24,           # X
    'dispute_status': 25,                 # Y
    'dispute_decision_notes': 26,         # Z
    'hr_ta_approval': 27,                 # AA
    'sales_approval': 28,                 # AB
    'quality_approval': 29,               # AC
    'ta_team_leader_approval': 30,        # AD
    'head_of_sales_approval': 31,         # AE
    'send_to_offer_at': 32,               # AF
    'offer_sent_at': 33,                  # AG
    'offer_status': 34,                   # AH
    'offer_decision': 35,                 # AI
    'final_candidate_status': 36,         # AJ
    'previous_test_call_score': 37,       # AK
    'previous_test_call_result': 38,      # AL
    'previous_test_call_evaluation': 39,  # AM
    'regeneration_count': 40,             # AN
    'test_session_id': 41,                # AO
}


class GoogleSheetsClient:
    """Client for reading/writing HiringFlow Google Sheets."""

    def __init__(self):
        self.sheet_id = os.getenv('GOOGLE_SHEET_ID', '').strip()
        creds_path = os.getenv('GOOGLE_CREDENTIALS', '').strip()

        if not self.sheet_id:
            raise ValueError("GOOGLE_SHEET_ID environment variable is required")
        if not creds_path:
            raise ValueError("GOOGLE_CREDENTIALS environment variable is required")

        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        self.client = gspread.authorize(creds)
        self.sheet = self.client.open_by_key(self.sheet_id)

    def get_heads_user(self, email: str, access_code: str) -> Optional[Dict[str, Any]]:
        """Lookup internal user from Heads sheet.

        Returns user dict with name, email, queue, role if found and active.
        Returns None if not found or inactive.
        """
        try:
            heads = self.sheet.worksheet('Heads')
            records = heads.get_all_records()

            for row in records:
                row_email = str(row.get('Email', '')).strip().lower()
                row_code = str(row.get('AccessCode', '')).strip()
                row_status = str(row.get('Status', '')).strip().lower()

                if (row_email == email.lower().strip() and
                    row_code == access_code.strip() and
                    row_status == 'active'):
                    return {
                        'name': row.get('Name', ''),
                        'email': row.get('Email', ''),
                        'queue': row.get('Queue', ''),
                        'role': row.get('Role', ''),
                    }
            return None
        except Exception as e:
            logger.error("Failed to read Heads sheet: %s", e)
            return None

    def get_candidate(self, email: str, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Lookup candidate from Candidates sheet.

        Returns candidate dict if found.
        Returns None if not found.
        """
        try:
            candidates = self.sheet.worksheet('Candidates')
            records = candidates.get_all_records()

            for row in records:
                row_email = str(row.get('CandidateEmail', '')).strip().lower()
                row_id = str(row.get('CandidateID', '')).strip()

                if (row_email == email.lower().strip() and
                    row_id == candidate_id.strip()):
                    return {
                        'candidate_id': row.get('CandidateID', ''),
                        'candidate_name': row.get('CandidateName', ''),
                        'candidate_email': row.get('CandidateEmail', ''),
                        'scenario': 'new-lead-discovery-call',  # Default scenario
                        'test_call_status': row.get('TestCallStatus', 'pending'),
                        'test_call_started_at': row.get('TestCallStartedAt', ''),
                        'test_call_ended_at': row.get('TestCallEndedAt', ''),
                        'score': row.get('TestCallScore', ''),
                        'result': row.get('TestCallResult', ''),
                        'regeneration_count': int(row.get('RegenerationCount', 0) or 0),
                        'test_session_id': row.get('TestSessionId', ''),
                        'queue': row.get('Queue', ''),
                    }
            return None
        except Exception as e:
            logger.error("Failed to read Candidates sheet: %s", e)
            return None

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

    def increment_regeneration(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Create new candidate row for regeneration, preserve old attempt.

        Returns:
            New candidate data dict, or None if failed
        """
        try:
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

            # Create new candidate row
            new_candidate_id = f"CAND-{uuid.uuid4().hex[:8].upper()}"
            new_row = [
                '',  # CreatedAt (A)
                new_candidate_id,  # CandidateID (B)
                old_candidate.get('CandidateName', ''),  # CandidateName (C)
                old_candidate.get('CandidateEmail', ''),  # CandidateEmail (D)
                old_candidate.get('PhoneNumber', ''),  # PhoneNumber (E)
                '',  # CVUrl (F)
                old_candidate.get('Queue', ''),  # Queue (G)
                old_candidate.get('Batch', ''),  # Batch (H)
                old_candidate.get('SubBatch', ''),  # SubBatch (I)
                old_candidate.get('TAEmail', ''),  # TAEmail (J)
                '',  # AcceptanceEmailScheduledAt (K)
                '',  # AcceptanceEmailSentAt (L)
                '',  # AcceptanceEmailStatus (M)
                '',  # BookingWindowStart (N)
                '',  # BookingWindowEnd (O)
                '',  # TestCallScheduledAt (P)
                0,   # RescheduleCount (Q)
                'pending',  # TestCallStatus (R)
                '',  # TestCallLinkSentAt (S)
                '',  # TestCallStartedAt (T)
                '',  # TestCallEndedAt (U)
                '',  # TestCallScore (V)
                '',  # TestCallResult (W)
                '',  # TestCallEvaluation (X)
                '',  # DisputeStatus (Y)
                '',  # DisputeDecisionNotes (Z)
                '',  # HRTAApproval (AA)
                '',  # SalesApproval (AB)
                '',  # QualityApproval (AC)
                '',  # TATeamLeaderApproval (AD)
                '',  # HeadOfSalesApproval (AE)
                '',  # SendToOfferAt (AF)
                '',  # OfferSentAt (AG)
                '',  # OfferStatus (AH)
                '',  # OfferDecision (AI)
                '',  # FinalCandidateStatus (AJ)
                '',  # PreviousTestCallScore (AK)
                '',  # PreviousTestCallResult (AL)
                '',  # PreviousTestCallEvaluation (AM)
                new_count,  # RegenerationCount (AN)
                f"TEST-{uuid.uuid4().hex[:8].upper()}",  # TestSessionId (AO)
            ]

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
            candidates = self.sheet.worksheet('Candidates')

            candidate_id = candidate_data.get('candidate_id', f"CAND-{uuid.uuid4().hex[:8].upper()}")

            new_row = [
                '',  # CreatedAt (A)
                candidate_id,  # CandidateID (B)
                candidate_data.get('candidate_name', ''),  # CandidateName (C)
                candidate_data.get('candidate_email', ''),  # CandidateEmail (D)
                candidate_data.get('phone_number', ''),  # PhoneNumber (E)
                '',  # CVUrl (F)
                candidate_data.get('queue', ''),  # Queue (G)
                candidate_data.get('batch', ''),  # Batch (H)
                candidate_data.get('sub_batch', ''),  # SubBatch (I)
                candidate_data.get('ta_email', ''),  # TAEmail (J)
                '',  # AcceptanceEmailScheduledAt (K)
                '',  # AcceptanceEmailSentAt (L)
                '',  # AcceptanceEmailStatus (M)
                '',  # BookingWindowStart (N)
                '',  # BookingWindowEnd (O)
                '',  # TestCallScheduledAt (P)
                0,   # RescheduleCount (Q)
                'pending',  # TestCallStatus (R)
                '',  # TestCallLinkSentAt (S)
                '',  # TestCallStartedAt (T)
                '',  # TestCallEndedAt (U)
                '',  # TestCallScore (V)
                '',  # TestCallResult (W)
                '',  # TestCallEvaluation (X)
                '',  # DisputeStatus (Y)
                '',  # DisputeDecisionNotes (Z)
                '',  # HRTAApproval (AA)
                '',  # SalesApproval (AB)
                '',  # QualityApproval (AC)
                '',  # TATeamLeaderApproval (AD)
                '',  # HeadOfSalesApproval (AE)
                '',  # SendToOfferAt (AF)
                '',  # OfferSentAt (AG)
                '',  # OfferStatus (AH)
                '',  # OfferDecision (AI)
                '',  # FinalCandidateStatus (AJ)
                '',  # PreviousTestCallScore (AK)
                '',  # PreviousTestCallResult (AL)
                '',  # PreviousTestCallEvaluation (AM)
                0,   # RegenerationCount (AN)
                f"TEST-{uuid.uuid4().hex[:8].upper()}",  # TestSessionId (AO)
            ]

            candidates.append_row(new_row)
            logger.info("Created candidate row: %s", candidate_id)
            return candidate_id
        except Exception as e:
            logger.error("Failed to create candidate row: %s", e)
            return None


# Singleton client
_sheets_client: Optional[GoogleSheetsClient] = None


def get_sheets_client() -> GoogleSheetsClient:
    """Get or create Google Sheets client singleton."""
    global _sheets_client
    if _sheets_client is None:
        _sheets_client = GoogleSheetsClient()
    return _sheets_client
