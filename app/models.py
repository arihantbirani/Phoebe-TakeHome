from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class Caregiver(BaseModel):
    id: str
    name: str
    role: str
    phone: str


class Shift(BaseModel):
    id: str
    organization_id: str
    role_required: str
    start_time: datetime
    end_time: datetime
    claimed_by_caregiver_id: str | None = None


class ContactChannel(StrEnum):
    SMS = "sms"
    CALL = "call"


class ShiftFanoutState(BaseModel):
    shift_id: str
    # When fanout was first triggered (for the 10-minute escalation clock)
    started_at: datetime
    # IDs of caregivers we attempted via SMS
    sms_notified_caregiver_ids: list[str] = []
    # IDs of caregivers we attempted via phone call
    call_notified_caregiver_ids: list[str] = []
    # Caregiver who successfully claimed the shift, if any
    claimed_caregiver_id: str | None = None
    # Whether escalation to phone has already happened
    escalated_to_call: bool = False
