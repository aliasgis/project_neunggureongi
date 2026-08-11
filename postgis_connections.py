from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.engine import URL


class PostgisConnectionStore:
    def __init__(self, base_dir: Path, key_provider):
        self.path = base_dir / "data" / "postgis_connections.bin"
        self.key_provider = key_provider
        self.lock = threading.RLock()

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            payload = Fernet(self.key_provider()).decrypt(self.path.read_bytes())
            return json.loads(payload.decode("utf-8"))
        except (OSError, InvalidToken, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("PostGIS 연결 정보를 읽을 수 없습니다.") from error

    def _write(self, connections: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(connections, ensure_ascii=False).encode("utf-8")
        encrypted = Fernet(self.key_provider()).encrypt(payload)
        temporary = self.path.with_suffix(".bin.tmp")
        temporary.write_bytes(encrypted)
        temporary.replace(self.path)

    def list(self, include_password: bool = False) -> list[dict]:
        with self.lock:
            items = self._read()
        if include_password:
            return items
        return [{**item, "password": "", "has_password": bool(item.get("password"))} for item in items]

    def get(self, connection_id: str) -> dict:
        item = next((x for x in self.list(True) if x["id"] == connection_id), None)
        if not item:
            raise KeyError(f"Unknown PostGIS connection: {connection_id}")
        return item

    def save(self, data: dict, original_id: str | None = None) -> dict:
        item = {key: str(data.get(key, "")).strip() for key in ("id", "name", "host", "port", "database", "username", "password", "sslmode")}
        if original_id and item["id"] != original_id:
            raise ValueError("사용 중인 연결 ID는 변경할 수 없습니다.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", item["id"]):
            raise ValueError("연결 ID는 영문, 숫자, _, -, .만 사용할 수 있습니다.")
        for key in ("name", "host", "database", "username"):
            if not item[key]:
                raise ValueError(f"{key} 값이 필요합니다.")
        try:
            port = int(item["port"] or 5432)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError as error:
            raise ValueError("포트는 1~65535 사이여야 합니다.") from error
        item["port"] = str(port);item["sslmode"] = item["sslmode"] or "prefer"
        with self.lock:
            items = self._read()
            existing = next((x for x in items if x["id"] == original_id), None) if original_id else None
            if existing and not item["password"]:
                item["password"] = existing.get("password", "")
            if not item["password"]:
                raise ValueError("비밀번호가 필요합니다.")
            if any(x["id"] == item["id"] and x["id"] != original_id for x in items):
                raise ValueError("이미 존재하는 연결 ID입니다.")
            if existing:
                items[items.index(existing)] = item
            else:
                items.append(item)
            self._write(items)
        return {**item, "password": "", "has_password": True}

    def delete(self, connection_id: str) -> None:
        with self.lock:
            items = self._read();kept = [x for x in items if x["id"] != connection_id]
            if len(items) == len(kept):
                raise KeyError(connection_id)
            self._write(kept)

    def url(self, connection_id: str) -> URL:
        item = self.get(connection_id)
        return URL.create("postgresql+psycopg", username=item["username"], password=item["password"], host=item["host"], port=int(item["port"]), database=item["database"], query={"sslmode": item["sslmode"]})
