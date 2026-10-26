"""持久化、不含明文的写操作记录；未确认的写操作在进程重启后仍然有效。"""
from __future__ import annotations

import uuid
from .configuration import file_lock, read_json, write_json
from .contracts import (ErrorCategory, Feature, OperationStatus, UNRESOLVED_OPERATION_STATUSES, utc_now)
from .providers import ProviderFailure


class WriteJournal:
    def __init__(self, path, site_id, binding):
        self.path, self.site_id, self.binding = path, site_id, binding

    def entries(self):
        return (read_json(self.path, {}) or {}).get("operations", [])

    def _save(self, operations):
        write_json(self.path, {"schema_version": 1, "operations": operations})

    def prepare(self, *, feature, target_id, parameter_digest, config_revision, account_id):
        with file_lock(self.path):
            entries = self.entries()
            for entry in entries:
                # Same target cannot be changed while an earlier write is ambiguous.
                if (entry["site"] == self.site_id and entry["binding"] == self.binding
                        and str(entry["account_id"]) == str(account_id)
                        and entry["status"] in UNRESOLVED_OPERATION_STATUSES
                        and (target_id is not None and str(entry["target_id"]) == str(target_id)
                             or target_id is None and entry["feature"] == feature and entry["parameter_digest"] == parameter_digest)):
                    raise ProviderFailure(ErrorCategory.OPERATION_UNKNOWN, {"operation_id": entry["id"]})
            operation = uuid.uuid4().hex
            entries.append({"id": operation, "site": self.site_id, "binding": self.binding,
                            "account_id": account_id, "feature": feature, "target_id": target_id,
                            "parameter_digest": parameter_digest, "config_revision": config_revision,
                            "status": OperationStatus.PREPARED.value,
                            "created_at": utc_now(), "updated_at": utc_now()})
            self._save(entries)
            return operation

    def evidence(self, operation, value):
        self._change(operation, evidence=value)

    def sent(self, operation):
        self._change(operation, status=OperationStatus.SENT.value)

    def finish(self, operation, status, remote_id):
        self._change(operation, status=status, remote_id=remote_id)

    def _change(self, operation, **changes):
        with file_lock(self.path):
            entries = self.entries()
            entry = next(e for e in entries if e["id"] == operation and e["site"] == self.site_id and e["binding"] == self.binding)
            entry.update(changes, updated_at=utc_now())
            self._save(entries)

    def reconcile(self, operation, *, account_id, rows):
        """只有一次"完整且属于当前账户"的读取才能了结一条结果不明的写操作。"""
        with file_lock(self.path):
            entry = next((e for e in self.entries() if e["id"] == operation and e["site"] == self.site_id
                          and e["binding"] == self.binding and str(e["account_id"]) == str(account_id)), None)
            if entry is None:
                raise ProviderFailure(ErrorCategory.ACCOUNT_MISMATCH)
            remote = entry.get("remote_id") or entry.get("target_id")
            if entry["feature"] == Feature.KEYS_DELETE.value and not any(str(row["id"]) == str(remote) for row in rows):
                self.finish(operation, OperationStatus.CONFIRMED.value, remote)
                return True
            evidence = entry.get("evidence") or {}
            expected = evidence.get("expected")
            if expected:
                candidates = [row for row in rows if all(row.get(k) == v for k, v in expected.items())
                              and (str(row["id"]) == str(remote) if remote is not None else
                                   str(row["id"]) not in evidence.get("before_ids", []))]
                if len(candidates) == 1:
                    self.finish(operation, OperationStatus.CONFIRMED.value, candidates[0]["id"])
                    return True
            return False
