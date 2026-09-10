"""TenantHub: the registry plus one MunshiPlatform per business, opened on
first use and kept warm. Each business has its own SQLite file for data and
its own file for agent checkpoints, so nothing about tenant A is ever in a
query that touches tenant B — isolation is by file, not by WHERE clause."""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel

from munshi.auth import Registry
from munshi.domain.repository import MunshiRepository
from munshi.domain.seed import seed, seed_new_business
from munshi.platform import MunshiPlatform

DEMO_BUSINESS_ID = "demo"
DEMO_USERS = [   # (name, phone, role, pin)
    ("Sultan Ahmed", "0300-0000001", "owner", "1111"),
    ("Bilal Hussain", "0300-0000002", "clerk", "2222"),
    ("Rafiq Masih", "0300-0000003", "driver", "3333"),
    ("Imran Khan", "0300-0000004", "salesman", "4444"),
]


class TenantHub:
    def __init__(self, data_dir: str | None = None, model: BaseChatModel | None = None, enable_tracing: bool = False,
                 max_open: int = 64, in_memory: bool = False) -> None:
        self.in_memory = in_memory
        self.data_dir = Path(data_dir or os.environ.get("MUNSHI_DATA_DIR", "data"))
        if not in_memory:
            (self.data_dir / "tenants").mkdir(parents=True, exist_ok=True)
        self.registry = Registry(":memory:" if in_memory else str(self.data_dir / "registry.db"))
        self.model = model
        self.enable_tracing = enable_tracing
        self.max_open = max_open
        self._platforms: OrderedDict[str, MunshiPlatform] = OrderedDict()
        self._lock = threading.RLock()

    # ------------------------------------------------------------ paths
    def db_path(self, business_id: str) -> str:
        return ":memory:" if self.in_memory else str(self.data_dir / "tenants" / f"{business_id}.db")

    def checkpoint_path(self, business_id: str) -> str | None:
        return None if self.in_memory else str(self.data_dir / "tenants" / f"{business_id}.agents.db")

    # ------------------------------------------------------------ platforms
    def platform(self, business_id: str) -> MunshiPlatform:
        with self._lock:
            p = self._platforms.get(business_id)
            if p is not None:
                self._platforms.move_to_end(business_id)
                return p
            self.registry.get_business(business_id)
            repo = MunshiRepository(self.db_path(business_id))
            p = MunshiPlatform(repo, self.model, enable_tracing=self.enable_tracing, checkpoint_path=self.checkpoint_path(business_id))
            self._platforms[business_id] = p
            while len(self._platforms) > self.max_open:
                _, old = self._platforms.popitem(last=False)
                old.close()
            return p

    def close(self) -> None:
        with self._lock:
            for p in self._platforms.values():
                p.close()
            self._platforms.clear()

    # ------------------------------------------------------------ provisioning
    def signup(self, business_name: str, city: str, owner_name: str, phone: str, pin: str, sample_data: bool = False, language: str = "en") -> dict:
        """Create a business and its first owner. Returns {business, user, token}."""
        with self._lock:
            biz = self.registry.create_business(business_name, city, phone)
            try:
                user = self.registry.create_user(biz["business_id"], owner_name, phone, "owner", pin)
            except Exception:
                self.registry.update_business(biz["business_id"], active=0)
                raise
            repo = MunshiRepository(self.db_path(biz["business_id"]))
            if sample_data:
                seed(repo); repo.set_setting("business_name", business_name); repo.set_setting("city", city); repo.set_setting("owner_phone", user["phone"])
            else:
                seed_new_business(repo, business_name, city, phone, user["phone"], language)
            if self.in_memory:      # keep the seeded in-memory repo instead of opening a fresh one
                self._platforms[biz["business_id"]] = MunshiPlatform(repo, self.model, enable_tracing=self.enable_tracing)
            else:
                repo.close()
        token, principal, _ = self.registry.authenticate(phone, pin)
        return {"business": biz, "user": user, "token": token, "principal": principal}

    def ensure_demo(self) -> None:
        """The Sultan Traders demo business with four demo users. Idempotent."""
        with self._lock:
            try:
                self.registry.get_business(DEMO_BUSINESS_ID)
                return
            except Exception:
                pass
            self.registry.create_business("Sultan Traders", "Multan", "061-4567890", plan="demo", business_id=DEMO_BUSINESS_ID)
            for name, phone, role, pin in DEMO_USERS:
                self.registry.create_user(DEMO_BUSINESS_ID, name, phone, role, pin, allow_weak_pin=True)
            repo = MunshiRepository(self.db_path(DEMO_BUSINESS_ID))
            seed(repo)
            if self.in_memory:
                self._platforms[DEMO_BUSINESS_ID] = MunshiPlatform(repo, self.model, enable_tracing=self.enable_tracing)
            else:
                repo.close()

    def reset_demo(self) -> None:
        """Wipe and reseed the demo business (a cron job on the public demo)."""
        with self._lock:
            p = self._platforms.pop(DEMO_BUSINESS_ID, None)
            if p: p.close()
            if not self.in_memory:
                for suffix in (".db", ".db-wal", ".db-shm", ".agents.db", ".agents.db-wal", ".agents.db-shm"):
                    f = self.data_dir / "tenants" / f"{DEMO_BUSINESS_ID}{suffix}"
                    if f.exists(): f.unlink()
                repo = MunshiRepository(self.db_path(DEMO_BUSINESS_ID)); seed(repo); repo.close()
            else:
                self._platforms[DEMO_BUSINESS_ID] = MunshiPlatform(seed(MunshiRepository(":memory:")), self.model, enable_tracing=self.enable_tracing)
