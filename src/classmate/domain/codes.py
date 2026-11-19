import hashlib
import secrets


def generate_code(length: int = 10) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def hash_code(value: str) -> str:
    return hashlib.sha256(value.strip().upper().encode("utf-8")).hexdigest()
