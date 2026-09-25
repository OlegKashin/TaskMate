from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.security import verify_user_token
from app.db.session import get_db
from app.models.entities import User
from app.services.domain import UserService

bearer = HTTPBearer(auto_error=False)


def current_user(
    db: Annotated[Session, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    if not credentials:
        raise AppError("AUTH_ERROR", "Invalid API credentials", 401)
    telegram_user_id = verify_user_token(credentials.credentials)
    return UserService(db).get_or_create(telegram_user_id)


DB = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(current_user)]
