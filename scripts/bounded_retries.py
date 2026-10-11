"""Persistent attempt limits shared by independent writer and review parts."""
import time
from pathlib import Path

from summary_workflow import digest, read, write


def inherited_part_attempts(directory, binding, previous_bindings):
    root = Path(directory) / "dispatch-ledgers"
    if (root / f"{binding}.json").exists():
        return []
    rows, seen = [], set()
    for old in previous_bindings:
        path = root / f"{old}.json"
        if not path.exists():
            continue
        ledger = read(path)
        if ledger.get("binding") != old or not isinstance(ledger.get("attempts"), list):
            raise ValueError("Inherited part retry ledger is corrupt")
        for row in ledger["attempts"]:
            if not isinstance(row, dict):
                raise ValueError("Inherited part retry ledger is corrupt")
            identity = digest({k: v for k, v in row.items() if k not in {"attempt", "original_attempt", "migrated_from"}})
            if identity in seen:
                continue
            seen.add(identity)
            rows.append({**row, "original_attempt": row["attempt"], "migrated_from": str(path)})
    rows.sort(key=lambda row: row.get("started_at", 0))
    return [{**row, "attempt": i + 1} for i, row in enumerate(rows)]


def bounded_part_attempts(directory, binding, execute, previous_attempts=None):
    path = Path(directory) / "dispatch-ledgers" / f"{binding}.json"
    ledger = read(path) if path.exists() else {"binding": binding, "attempts": previous_attempts or []}
    rows = ledger.get("attempts")
    if (ledger.get("binding") != binding or not isinstance(rows, list) or len(rows) > 3
            or any(not isinstance(row, dict) or row.get("attempt") != i + 1
                   or row.get("status") not in {"running", "failed", "passed"}
                   for i, row in enumerate(rows))):
        raise ValueError("Part retry ledger is corrupt")
    if previous_attempts and not path.exists():
        write(path, ledger)
    for index in range(len(rows), 3):
        row = {"attempt": index + 1, "status": "running", "started_at": time.time()}
        rows.append(row)
        write(path, ledger)
        try:
            value = execute(index + 1)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            row.update(status="failed", error=str(exc), finished_at=time.time())
            write(path, ledger)
            if isinstance(exc, OSError):
                raise
        else:
            row.update(status="passed", output_hash=digest(value), finished_at=time.time())
            write(path, ledger)
            return value
    raise ValueError("Part retry limit exhausted: " + rows[-1].get("error", "Previous part output needs repair"))
