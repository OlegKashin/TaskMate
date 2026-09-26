from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.deps import DB, CurrentUser
from app.core.errors import AppError
from app.services.oauth import OAuthService

router = APIRouter(prefix="/api/v1/oauth")


@router.post("/{provider}/states", status_code=201)
def create_oauth_state(provider: str, db: DB, user: CurrentUser, account_email: str | None = None):
    state = OAuthService(db).create_state(user, provider, account_email)
    return {
        "data": {
            "state": state.state_token,
            "authorize_url": f"/api/v1/oauth/{provider}/authorize?state={state.state_token}",
        },
        "meta": {},
    }


@router.get("/{provider}/authorize")
def oauth_authorize(provider: str, state: str, db: DB):
    return RedirectResponse(OAuthService(db).authorize_url(provider, state), status_code=302)


@router.get("/{provider}/callback")
def oauth_callback(
    provider: str, state: str, db: DB, code: str | None = None, error: str | None = None
):
    service = OAuthService(db)
    if error:
        try:
            service.fail(provider, state)
        except AppError:
            pass
        return HTMLResponse(
            "<html><body><h1>Подключение отменено</h1>"
            "<p>Вернитесь в Telegram и повторите попытку.</p></body></html>",
            status_code=400,
        )
    try:
        result = service.complete(provider, state, code or "")
    except AppError as exc:
        return HTMLResponse(
            f"<html><body><h1>Не удалось подключить</h1><p>{escape(exc.message)}</p></body></html>",
            status_code=exc.status_code,
        )
    return HTMLResponse(
        f"<html><body><h1>Подключено</h1>"
        f"<p>{escape(provider)}: {escape(str(result.id))}</p>"
        "<p>Можно вернуться в Telegram.</p></body></html>"
    )
