from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


PBKDF2_ITERATIONS = 600_000
SESSION_MAX_AGE = 8 * 60 * 60


class AdminAuth:
    def __init__(self, base_dir: Path):
        self.key_path = base_dir / ".admin.key"
        self.credentials_path = base_dir / "data" / "admin_credentials.bin"

    def is_configured(self) -> bool:
        return self.key_path.exists() and self.credentials_path.exists()

    def _key(self, create: bool = False) -> bytes:
        environment_key = os.getenv("ADMIN_ENCRYPTION_KEY")
        if environment_key:
            return environment_key.encode("ascii")
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        if not create:
            raise RuntimeError("Admin credentials are not configured")
        key = Fernet.generate_key()
        self.key_path.write_bytes(key)
        return key

    def set_credentials(self, username: str, password: str) -> None:
        username = username.strip()
        if not username or len(username) > 100:
            raise ValueError("ID는 1~100자로 입력해 주세요.")
        if len(password) < 8:
            raise ValueError("비밀번호는 8자 이상이어야 합니다.")
        salt = secrets.token_bytes(16)
        password_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
        )
        payload = json.dumps(
            {
                "username": username,
                "salt": base64.b64encode(salt).decode("ascii"),
                "password_hash": base64.b64encode(password_hash).decode("ascii"),
                "updated_at": int(time.time()),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        encrypted = Fernet(self._key(create=True)).encrypt(payload)
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.credentials_path.with_suffix(".bin.tmp")
        temporary.write_bytes(encrypted)
        temporary.replace(self.credentials_path)

    def reset_credentials(self, username: str, password: str) -> Path | None:
        """Rotate the encryption key and replace credentials, preserving a backup."""
        backup_directory = None
        if self.key_path.exists() or self.credentials_path.exists():
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            backup_directory = (
                self.credentials_path.parent / "admin_auth_backups" / timestamp
            )
            backup_directory.mkdir(parents=True, exist_ok=True)
            if self.key_path.exists():
                shutil.move(str(self.key_path), backup_directory / "admin.key")
            if self.credentials_path.exists():
                shutil.move(
                    str(self.credentials_path),
                    backup_directory / "admin_credentials.bin",
                )
        try:
            self.set_credentials(username, password)
        except Exception:
            if backup_directory:
                backup_key = backup_directory / "admin.key"
                backup_credentials = backup_directory / "admin_credentials.bin"
                if backup_key.exists():
                    shutil.move(str(backup_key), self.key_path)
                if backup_credentials.exists():
                    shutil.move(str(backup_credentials), self.credentials_path)
            raise
        return backup_directory

    def _credentials(self) -> dict:
        try:
            encrypted = self.credentials_path.read_bytes()
            decrypted = Fernet(self._key()).decrypt(encrypted)
            return json.loads(decrypted.decode("utf-8"))
        except (OSError, InvalidToken, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("관리자 인증 정보를 읽을 수 없습니다.") from error

    def authenticate(self, username: str, password: str) -> bool:
        try:
            credentials = self._credentials()
        except RuntimeError:
            return False
        salt = base64.b64decode(credentials["salt"])
        expected = base64.b64decode(credentials["password_hash"])
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
        )
        username_matches = hmac.compare_digest(
            username.encode("utf-8"), credentials["username"].encode("utf-8")
        )
        return username_matches and hmac.compare_digest(actual, expected)

    def create_session(self, username: str) -> str:
        timestamp = str(int(time.time()))
        message = f"{username}|{timestamp}".encode("utf-8")
        signature = hmac.new(self._key(), message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(message + b"|" + signature).decode("ascii")

    def verify_session(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            decoded = base64.urlsafe_b64decode(token.encode("ascii"))
            username_bytes, timestamp_bytes, signature = decoded.split(b"|", 2)
            message = username_bytes + b"|" + timestamp_bytes
            expected = hmac.new(self._key(), message, hashlib.sha256).digest()
            age = int(time.time()) - int(timestamp_bytes)
            return 0 <= age <= SESSION_MAX_AGE and hmac.compare_digest(
                signature, expected
            )
        except (ValueError, OSError, RuntimeError):
            return False
