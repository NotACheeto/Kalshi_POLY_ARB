"""
Execution Journal and State Reconciler for Crash Recovery.
Maintains an append-only JSONL Write-Ahead Log (WAL) of all trade transitions.
On startup, recovers state and alerts on any unhedged or dangling orders.
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from src.models import ExecutionState, LiveOrder

logger = logging.getLogger(__name__)


class ExecutionReconciler:
    """
    Persists execution state transitions and recovers system state on startup.
    Ensures that a process crash never leaves behind untracked unhedged exposure.
    """

    def __init__(self, journal_path: str = "logs/execution_journal.jsonl"):
        self.journal_path = Path(journal_path)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._active_sessions: Dict[str, Dict[str, Any]] = {}

    def log_transition(
        self,
        opportunity_id: str,
        pair_id: str,
        from_state: ExecutionState,
        to_state: ExecutionState,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append an atomic state transition to the journal file."""
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "timestamp": now,
            "opportunity_id": opportunity_id,
            "pair_id": pair_id,
            "from_state": from_state.value,
            "to_state": to_state.value,
            "metadata": metadata or {},
        }

        # Update in-memory session tracker
        if to_state in (ExecutionState.COMPLETED, ExecutionState.ABORTED, ExecutionState.FAILED):
            self._active_sessions.pop(opportunity_id, None)
        else:
            self._active_sessions[opportunity_id] = entry

        # Append to journal file (flush immediately)
        try:
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"Failed to write to execution journal: {e}")

    def recover_on_startup(self) -> List[Dict[str, Any]]:
        """
        Scan execution journal for unresolved trade sessions from previous runs.
        Returns list of dangling/unhedged sessions that require operator intervention.
        """
        if not self.journal_path.is_file():
            logger.info("No prior execution journal found. Clean state.")
            return []

        open_sessions: Dict[str, Dict[str, Any]] = {}

        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        opp_id = entry.get("opportunity_id")
                        to_state = entry.get("to_state")
                        if to_state in (
                            ExecutionState.COMPLETED.value,
                            ExecutionState.ABORTED.value,
                            ExecutionState.FAILED.value,
                        ):
                            open_sessions.pop(opp_id, None)
                        else:
                            open_sessions[opp_id] = entry
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"Error reading journal during crash recovery: {e}")

        unresolved = list(open_sessions.values())
        if unresolved:
            logger.critical(
                f"CRASH RECOVERY: Found {len(unresolved)} UNRESOLVED trade sessions from previous run! "
                f"Immediate manual reconciliation required: {unresolved}"
            )
        else:
            logger.info("Crash recovery complete: 0 unhedged sessions found from prior runs.")

        return unresolved
