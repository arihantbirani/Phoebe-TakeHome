from app.database import InMemoryKeyValueDatabase
from app.models import Caregiver, Shift, ShiftFanoutState

# In-memory databases
caregiver_db = InMemoryKeyValueDatabase[str, Caregiver]()
shift_db = InMemoryKeyValueDatabase[str, Shift]()
fanout_db = InMemoryKeyValueDatabase[str, ShiftFanoutState]()
