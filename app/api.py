import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel

from app.database import InMemoryKeyValueDatabase
from app.intent import parse_shift_request_message_intent, ShiftRequestMessageIntent
from app.models import Caregiver, Shift, ShiftFanoutState
from app.notifier import place_phone_call, send_sms
from app.state import caregiver_db, fanout_db, shift_db

# --- Helpers ---

def load_sample_data() -> None:
    """
    Load sample data from sample_data.json into the in-memory databases.
    """
    # Assuming sample_data.json is in the project root, one level up from app/
    base_path = Path(__file__).resolve().parent.parent
    data_path = base_path / "sample_data.json"
    
    if not data_path.exists():
        # Fallback for running tests if CWD is different or structure varies
        data_path = Path("sample_data.json")

    with open(data_path, "r") as f:
        data = json.load(f)

    for c_data in data.get("caregivers", []):
        caregiver = Caregiver(**c_data)
        caregiver_db.put(caregiver.id, caregiver)

    for s_data in data.get("shifts", []):
        shift = Shift(**s_data)
        shift_db.put(shift.id, shift)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_sample_data()
    yield
    # Cleanup if needed (clearing DBs)
    caregiver_db.clear()
    shift_db.clear()
    fanout_db.clear()


router = APIRouter()


# --- Models for Endpoints ---

class InboundMessage(BaseModel):
    from_phone: str
    shift_id: str
    body: str


class ShiftClaimResponse(BaseModel):
    success: bool
    message: str
    shift_id: str
    claimed_by: str | None


# --- Endpoints ---

@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/shifts/{shift_id}/fanout")
async def trigger_fanout(shift_id: str) -> ShiftFanoutState:
    shift = shift_db.get(shift_id)
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found")

    # Get or create fanout state
    state = fanout_db.get(shift_id)
    if not state:
        state = ShiftFanoutState(shift_id=shift_id, started_at=datetime.now(UTC))
        # We need to save it immediately if we want to persist the started_at time properly 
        # for race safety, but here it's fine as we are single threaded mostly.
        fanout_db.put(shift_id, state)
    
    # If already claimed, return state
    if state.claimed_caregiver_id:
        return state

    # Identify eligible caregivers
    eligible_caregivers = [
        c for c in caregiver_db.all() 
        if c.role == shift.role_required
    ]
    eligible_ids = {c.id for c in eligible_caregivers}

    # 1. Immediate SMS round
    # Send SMS if not already sent
    sms_tasks = []
    for caregiver in eligible_caregivers:
        if caregiver.id not in state.sms_notified_caregiver_ids:
            state.sms_notified_caregiver_ids.append(caregiver.id)
            sms_tasks.append(send_sms(caregiver.phone, f"New shift available! ID: {shift.id}"))
    
    if sms_tasks:
        await asyncio.gather(*sms_tasks)
        fanout_db.put(shift_id, state)

    # 2. Escalation check
    now = datetime.now(UTC)
    if (now - state.started_at) >= timedelta(minutes=10) and not state.escalated_to_call:
        state.escalated_to_call = True
        call_tasks = []
        for caregiver in eligible_caregivers:
            # We call everyone eligible, even if they were SMS'd (implied by "escalation")
            # But we check if we ALREADY called them to be idempotent
            if caregiver.id not in state.call_notified_caregiver_ids:
                state.call_notified_caregiver_ids.append(caregiver.id)
                call_tasks.append(place_phone_call(caregiver.phone, f"Urgent: Shift available! ID: {shift.id}"))
        
        if call_tasks:
            await asyncio.gather(*call_tasks)

        fanout_db.put(shift_id, state)

    return state


@router.post("/messages/inbound")
async def receive_message(msg: InboundMessage) -> ShiftClaimResponse:
    # 1. Find caregiver
    caregiver = next((c for c in caregiver_db.all() if c.phone == msg.from_phone), None)
    if not caregiver:
        # Per requirements, maybe 404/400. Let's return 404 for unknown user.
        raise HTTPException(status_code=404, detail="Caregiver not found")

    # 2. Find shift
    shift = shift_db.get(msg.shift_id)
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found")
    
    # 3. Check eligibility
    if shift.role_required != caregiver.role:
         return ShiftClaimResponse(
             success=False, 
             message="Not eligible for this shift role", 
             shift_id=shift.id, 
             claimed_by=shift.claimed_by_caregiver_id
         )

    # 4. Parse intent
    intent = await parse_shift_request_message_intent(msg.body)
    
    if intent == ShiftRequestMessageIntent.ACCEPT:
        # CRITICAL SECTION - No Await
        fanout_state = fanout_db.get(shift.id)
        if not fanout_state:
            # Should technically not happen if fanout started, but handled just in case
            fanout_state = ShiftFanoutState(shift_id=shift.id, started_at=datetime.now(UTC))
            fanout_db.put(shift.id, fanout_state)
        
        if fanout_state.claimed_caregiver_id is not None:
             # Already claimed
             return ShiftClaimResponse(
                 success=False, 
                 message="Shift already claimed", 
                 shift_id=shift.id, 
                 claimed_by=fanout_state.claimed_caregiver_id
             )
        else:
            # WE WIN
            fanout_state.claimed_caregiver_id = caregiver.id
            fanout_db.put(shift.id, fanout_state)
            
            shift.claimed_by_caregiver_id = caregiver.id
            shift_db.put(shift.id, shift)
            
            return ShiftClaimResponse(
                success=True,
                message="Shift successfully claimed!",
                shift_id=shift.id,
                claimed_by=caregiver.id
            )

    elif intent == ShiftRequestMessageIntent.DECLINE:
        return ShiftClaimResponse(
            success=False, 
            message="Shift declined", 
            shift_id=shift.id, 
            claimed_by=shift.claimed_by_caregiver_id
        )
    else:
        return ShiftClaimResponse(
            success=False, 
            message="Intent unknown", 
            shift_id=shift.id, 
            claimed_by=shift.claimed_by_caregiver_id
        )


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    return app
