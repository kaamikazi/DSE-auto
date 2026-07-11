import hmac

from fastapi import Header, HTTPException, status

from app.core.config import get_settings


def require_api_key(x_api_key: str = Header(default="")) -> None:
    expected = get_settings().API_SECRET_KEY
    if not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
