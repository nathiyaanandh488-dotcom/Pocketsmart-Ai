"""auth.py - password hashing, JWT tokens and current-user dependencies."""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import Request

import database

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 8
COOKIE_NAME = "access_token"


class NotAuthenticated(Exception):
    """Raised when a protected route is called without a valid token."""


def hash_password(password: str) -> str:
    # bcrypt only uses the first 72 bytes of a password
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


def authenticate_user(username: str, password: str) -> Optional[dict]:
    user = database.get_user_by_username(username)
    if user and verify_password(password, user["hashed_password"]):
        return user
    return None


def create_access_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def _token_from_request(request: Request) -> Optional[str]:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(COOKIE_NAME)


def get_optional_user(request: Request) -> Optional[dict]:
    """Return the logged-in user or None (for public pages)."""
    token = _token_from_request(request)
    payload = decode_token(token) if token else None
    if not payload:
        return None
    return database.get_user_by_username(payload.get("sub", ""))


def get_current_user(request: Request) -> dict:
    """Dependency for protected routes."""
    user = get_optional_user(request)
    if not user:
        raise NotAuthenticated()
    return user
