from __future__ import annotations

import getpass
import sys
from pathlib import Path

from admin_auth import AdminAuth


def safe_input(prompt: str) -> str:
    """Ignore stale virtual-environment commands injected into console input."""
    while True:
        value = input(prompt).strip()
        normalized = value.replace("/", "\\").lower()
        if normalized.endswith(r"\venv\scripts\activate.bat"):
            print("[INFO] Ignored a stale activate.bat console command.")
            continue
        return value


def read_new_credentials() -> tuple[str, str]:
    username = safe_input("New admin ID: ")
    password = getpass.getpass("New password (8+ characters): ")
    confirmation = getpass.getpass("Confirm new password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    return username, password


def main() -> None:
    auth = AdminAuth(Path(__file__).resolve().parent)
    reset = len(sys.argv) > 1 and sys.argv[1].lower() == "reset"
    if reset:
        print("This resets the admin ID/password and invalidates every login session.")
        if safe_input("Type RESET to continue: ").upper() != "RESET":
            raise SystemExit("Reset cancelled.")
        username, password = read_new_credentials()
        backup = auth.reset_credentials(username, password)
        print("Admin credentials have been reset.")
        if backup:
            print(f"Previous encrypted credentials backed up to: {backup}")
    else:
        username, password = read_new_credentials()
        auth.set_credentials(username, password)
        print("Admin credentials saved to encrypted binary storage.")


if __name__ == "__main__":
    main()
