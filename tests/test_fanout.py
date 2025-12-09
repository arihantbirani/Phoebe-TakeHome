import pytest
import pytest_asyncio
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, patch, ANY
from freezegun import freeze_time
from httpx import AsyncClient, ASGITransport
from app.api import create_app
from app.state import caregiver_db, shift_db, fanout_db
from app.models import Caregiver, Shift, ShiftFanoutState, ContactChannel

@pytest_asyncio.fixture
async def client():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

@pytest.fixture
def clean_db():
    caregiver_db.clear()
    shift_db.clear()
    fanout_db.clear()
    
    # Setup standard test data
    c1 = Caregiver(id="c1", name="Alice", role="RN", phone="+15551111")
    c2 = Caregiver(id="c2", name="Bob", role="LPN", phone="+15552222")
    c3 = Caregiver(id="c3", name="Carol", role="RN", phone="+15553333")
    caregiver_db.put(c1.id, c1)
    caregiver_db.put(c2.id, c2)
    caregiver_db.put(c3.id, c3)
    
    s1 = Shift(
        id="s1", 
        organization_id="org1", 
        role_required="RN", 
        start_time=datetime.now(UTC), 
        end_time=datetime.now(UTC) + timedelta(hours=8)
    )
    shift_db.put(s1.id, s1)
    return s1

@pytest.mark.asyncio
async def test_fanout_sms_flow(client, clean_db):
    """
    Test that triggering fanout sends SMS to eligible caregivers only.
    """
    shift = clean_db
    
    with patch("app.api.send_sms", new_callable=AsyncMock) as mock_send_sms:
        with freeze_time("2025-01-01 12:00:00"):
            resp = await client.post(f"/shifts/{shift.id}/fanout")
            assert resp.status_code == 200
            data = resp.json()
            
            assert data["shift_id"] == shift.id
            assert data["started_at"].replace("+00:00", "Z") == "2025-01-01T12:00:00Z"
            assert not data["escalated_to_call"]
            
            # Eligible are c1 (RN) and c3 (RN). c2 (LPN) should not be contacted.
            assert "c1" in data["sms_notified_caregiver_ids"]
            assert "c3" in data["sms_notified_caregiver_ids"]
            assert "c2" not in data["sms_notified_caregiver_ids"]
            assert len(data["sms_notified_caregiver_ids"]) == 2
            
            # Verify Mock calls
            assert mock_send_sms.call_count == 2
            # Arguments are unordered in gathering, so checking any order
            calls = [c[0] for c in mock_send_sms.call_args_list]
            # calls should be (phone, message) tuples
            phones = {c[0] for c in calls}
            assert "+15551111" in phones # c1
            assert "+15553333" in phones # c3

            # Idempotency check: call again immediately
            resp2 = await client.post(f"/shifts/{shift.id}/fanout")
            data2 = resp2.json()
            assert data2 == data
            # Should NOT call send_sms again
            assert mock_send_sms.call_count == 2

@pytest.mark.asyncio
async def test_fanout_escalation(client, clean_db):
    """
    Test escalation to phone call after 10 minutes.
    """
    shift = clean_db
    
    with patch("app.api.send_sms", new_callable=AsyncMock) as mock_send_sms, \
         patch("app.api.place_phone_call", new_callable=AsyncMock) as mock_call:
        
        with freeze_time("2025-01-01 12:00:00") as frozen_time:
            # 1. Start fanout
            await client.post(f"/shifts/{shift.id}/fanout")
            assert mock_send_sms.call_count == 2
            
            # 2. Move 9 minutes forward -> No escalation yet
            frozen_time.tick(timedelta(minutes=9))
            resp = await client.post(f"/shifts/{shift.id}/fanout")
            assert not resp.json()["escalated_to_call"]
            assert mock_call.call_count == 0
            
            # 3. Move 1 more minute (total 10) -> Escalation
            frozen_time.tick(timedelta(minutes=1))
            resp = await client.post(f"/shifts/{shift.id}/fanout")
            data = resp.json()
            
            assert data["escalated_to_call"] is True
            assert "c1" in data["call_notified_caregiver_ids"]
            assert "c3" in data["call_notified_caregiver_ids"]
            assert mock_call.call_count == 2

@pytest.mark.asyncio
async def test_inbound_accept_claims_shift(client, clean_db):
    """
    Test accepting a shift.
    """
    shift = clean_db
    
    with patch("app.api.send_sms", new_callable=AsyncMock):
        # Trigger fanout first
        await client.post(f"/shifts/{shift.id}/fanout")

    # c1 accepts
    payload = {
        "from_phone": "+15551111", # Alice (RN)
        "shift_id": shift.id,
        "body": "Yes I can do it"
    }
    resp = await client.post("/messages/inbound", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["claimed_by"] == "c1"
    
    # Verify DB state
    s = shift_db.get(shift.id)
    assert s.claimed_by_caregiver_id == "c1"
    
    f = fanout_db.get(shift.id)
    assert f.claimed_caregiver_id == "c1"

@pytest.mark.asyncio
async def test_race_condition_second_accept_fails(client, clean_db):
    """
    Test that a second accept does not steal the shift.
    """
    shift = clean_db
    with patch("app.api.send_sms", new_callable=AsyncMock):
        await client.post(f"/shifts/{shift.id}/fanout")

    # c1 accepts
    await client.post("/messages/inbound", json={
        "from_phone": "+15551111",
        "shift_id": shift.id,
        "body": "Yes"
    })
    
    # c3 tries to accept
    resp = await client.post("/messages/inbound", json={
        "from_phone": "+15553333", # Carol (RN)
        "shift_id": shift.id,
        "body": "Accept please"
    })
    
    data = resp.json()
    assert data["success"] is False
    assert data["message"] == "Shift already claimed"
    assert data["claimed_by"] == "c1"

@pytest.mark.asyncio
async def test_inbound_decline(client, clean_db):
    """
    Test declining logic.
    """
    shift = clean_db
    resp = await client.post("/messages/inbound", json={
        "from_phone": "+15551111",
        "shift_id": shift.id,
        "body": "No thank you"
    })
    assert resp.json()["success"] is False
    assert resp.json()["message"] == "Shift declined"
    
    # Ensure not claimed
    s = shift_db.get(shift.id)
    assert s.claimed_by_caregiver_id is None

@pytest.mark.asyncio
async def test_inbound_unknown_number(client, clean_db):
    resp = await client.post("/messages/inbound", json={
        "from_phone": "+19999999",
        "shift_id": "s1",
        "body": "Yes"
    })
    assert resp.status_code == 404

@pytest.mark.asyncio
async def test_inbound_ineligible_role(client, clean_db):
    # c2 is LPN, shift requires RN
    resp = await client.post("/messages/inbound", json={
        "from_phone": "+15552222",
        "shift_id": "s1",
        "body": "Yes"
    })
    data = resp.json()
    assert data["success"] is False
    assert "Not eligible" in data["message"]
