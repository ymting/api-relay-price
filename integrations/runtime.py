"""CLI 与本地服务共用的凭据管理与提供者注册。"""
from __future__ import annotations

import copy
import threading
import uuid
from pathlib import Path

from .configuration import ConfigurationStore, credentials_of, digest, read_json
from .contracts import ConnectionPolicy, SiteConnectionPolicy
from .journal import WriteJournal
from .providers import CredentialSession, NewApiProvider, Sub2ApiProvider
from .retirement import EvidenceMatrix
from .router import Router


class IntegrationRuntime:
    def __init__(self, config_path, settings_path, data_dir, *, transport_factory=None):
        self.store = ConfigurationStore(config_path)
        self.settings_path, self.data_dir = Path(settings_path), Path(data_dir)
        self.transport_factory = transport_factory
        # 「站点 × 功能」调用证据，只写本地，供兼容退出判据使用。
        self.matrix = EvidenceMatrix(self.data_dir / "evidence_matrix.jsonl")
        self._lock, self._entries = threading.RLock(), {}

    def invalidate(self):
        with self._lock:
            for entry in self._entries.values():
                session = entry["session"]
                session.replace_credentials(entry["credentials"], uuid.uuid4().hex)
            self._entries.clear()

    def router(self, site, proxy="", *, settings=None, trigger="manual"):
        settings = settings if settings is not None else read_json(self.settings_path, {}) or {}
        policy = ConnectionPolicy.from_dict(settings.get("connection_policy"))
        key = (site["id"], proxy)
        with self._lock:
            entry = self._entries.get(key)
            credentials = credentials_of(site)
            fingerprint = digest([site, proxy, policy.to_dict()])
            if entry and entry["fingerprint"] != fingerprint:
                # An internal refresh changes only token fields/version, preserving this session.
                old = copy.deepcopy(entry["site"])
                snapshot = entry["session"].snapshot(entry["revision"])
                for name in ("access_token", "refresh_token", "expires_at"):
                    old.setdefault("auth", {})[name] = getattr(snapshot.credentials, name)
                old["credential_version"] = site.get("credential_version")
                old["_token_pair_ref"] = site.get("_token_pair_ref")
                if old == site and snapshot.credentials == credentials and entry["policy"] == policy.to_dict():
                    entry["fingerprint"], entry["site"] = fingerprint, copy.deepcopy(site)
                else:
                    entry["session"].replace_credentials(credentials, uuid.uuid4().hex)
                    entry = None
            if entry is None:
                revision = fingerprint
                session = CredentialSession(credentials, revision,
                    save_rotation=lambda expected, replacement: self.store.rotate(site, expected, replacement))
                binding = digest([site["id"], site["base_url"], site.get("type", "newapi")])
                entry = {"session": session, "revision": revision, "credentials": credentials,
                         "fingerprint": fingerprint, "site": copy.deepcopy(site), "policy": policy.to_dict(),
                         "journal": WriteJournal(self.data_dir / "write_operations.json", site["id"], binding), "compat": None}
                self._entries[key] = entry
            revision, session = entry["revision"], entry["session"]

        def current():
            try:
                snapshot = session.snapshot(revision)
                actual = next((s for s in self.store.sites() if s.get("id") == site["id"]), None)
                if actual is None:
                    return False
                expected = copy.deepcopy(site)
                for name in ("access_token", "refresh_token", "expires_at"):
                    if name in (actual.get("auth") or {}) or getattr(snapshot.credentials, name):
                        expected.setdefault("auth", {})[name] = getattr(snapshot.credentials, name)
                expected["credential_version"] = actual.get("credential_version")
                if "_token_pair_ref" in actual:
                    expected["_token_pair_ref"] = actual["_token_pair_ref"]
                comparable = copy.deepcopy(actual)
                comparable["credential_version"] = actual.get("credential_version")
                return expected == comparable and credentials_of(actual) == snapshot.credentials and (
                    ConnectionPolicy.from_dict((read_json(self.settings_path, {}) or {}).get("connection_policy")) == policy)
            except Exception:
                return False

        arguments = dict(config_revision=revision, proxy=proxy, write_hooks=entry["journal"])
        if self.transport_factory:
            arguments["transport"] = self.transport_factory(site, proxy)
        if site.get("type", "newapi") == "sub2api":
            api = Sub2ApiProvider(site, session, api_prefix=site.get("api_prefix", "/api/v1"),
                                  wallet_unit=site.get("wallet_unit", "USD"), **arguments)
        else:
            api = NewApiProvider(site, session, **arguments)

        def compatibility():
            with self._lock:
                if entry["compat"] is None:
                    from .compat import CompatProvider  # Global disable means no import/registration.
                    entry["compat"] = CompatProvider(site, proxy=proxy, config_revision=revision, write_hooks=entry["journal"])
                return entry["compat"]

        router = Router(api, policy=policy, site_policy=SiteConnectionPolicy.from_dict(site.get("connection")),
                        compat_factory=compatibility if policy.compat_enabled else None, is_current=current,
                        matrix=self.matrix, trigger=trigger)
        router.journal = entry["journal"]
        return router
