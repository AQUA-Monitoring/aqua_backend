"""Exchange verified Google identities for Aqua application users and JWTs."""
import hashlib
import secrets

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import Request
from google.oauth2 import id_token
import redis

from core.users.infra.models import User

SALT = "aqua.google.login.v1"
CHALLENGE_SECONDS = 300


class GoogleLoginError(Exception):
    def __init__(self, code, detail, status=401):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(detail)


def configuration():
    client_id = settings.GOOGLE_OAUTH_CLIENT_ID
    if not client_id:
        return {"enabled": False}
    nonce = secrets.token_urlsafe(32)
    challenge = signing.dumps({"nonce": nonce, "client_id": client_id}, salt=SALT)
    return {"enabled": True, "client_id": client_id, "nonce": nonce, "challenge": challenge}


class GoogleRequest(Request):
    def __call__(self, *args, **kwargs):
        kwargs["timeout"] = 10
        return super().__call__(*args, **kwargs)


def authenticate_google(credential, challenge):
    client_id = settings.GOOGLE_OAUTH_CLIENT_ID
    if not client_id:
        raise GoogleLoginError("google_unavailable", "O acesso com Google está indisponível no momento.", 503)
    try:
        state = signing.loads(challenge, salt=SALT, max_age=CHALLENGE_SECONDS)
        if state.get("client_id") != client_id:
            raise ValueError("client changed")
        claims = id_token.verify_oauth2_token(credential, GoogleRequest(), client_id)
        if not secrets.compare_digest(str(claims.get("nonce", "")), state["nonce"]):
            raise ValueError("nonce mismatch")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub or len(sub) > 255:
            raise ValueError("invalid subject")
        if claims.get("email_verified") is not True:
            raise ValueError("unverified email")
        email = str(claims.get("email", "")).strip().lower()
        validate_email(email)
        if len(email) > 300:
            raise ValueError("invalid email")
    except TransportError as exc:
        raise GoogleLoginError("google_unavailable", "Não foi possível confirmar sua conta com o Google. Tente novamente.", 503) from exc
    except (GoogleAuthError, ValueError, TypeError, KeyError, signing.BadSignature, ValidationError) as exc:
        raise GoogleLoginError("invalid_google_credential", "Não foi possível confirmar o acesso com Google. Tente novamente.") from exc

    # The shared cache makes one-time challenges work across all API processes.
    try:
        key = "aqua:google:used:" + hashlib.sha256(state["nonce"].encode()).hexdigest()
        with redis.Redis.from_url(settings.REDIS_CACHE_URL, socket_connect_timeout=2, socket_timeout=2) as cache:
            if not cache.set(key, "1", nx=True, ex=CHALLENGE_SECONDS):
                raise GoogleLoginError("google_challenge_used", "Esta tentativa já foi utilizada. Tente entrar novamente.")
    except redis.RedisError as exc:
        raise GoogleLoginError("google_unavailable", "O acesso com Google está temporariamente indisponível. Tente novamente.", 503) from exc

    try:
        with transaction.atomic():
            user = User.objects.select_for_update().filter(google_sub=sub).first()
            if user is None:
                matches = list(User.objects.select_for_update().filter(email__iexact=email)[:2])
                if len(matches) > 1:
                    raise GoogleLoginError("account_link_required", "Entre com email e senha para acessar sua conta.", 409)
                user = matches[0] if matches else None
                if user is not None:
                    # Google is authoritative for Gmail and verified Workspace emails.
                    authoritative = email.endswith("@gmail.com") or bool(claims.get("hd"))
                    if user.google_sub not in (None, "", sub) or not authoritative:
                        raise GoogleLoginError("account_link_required", "Este email já tem uma conta. Entre com email e senha.", 409)
                    if not user.is_active:
                        raise GoogleLoginError("account_inactive", "Esta conta está desativada.", 403)
                    user.google_sub = sub
                    user.save(update_fields=["google_sub", "updated_at"])
                else:
                    name = str(claims.get("name") or email.split("@")[0])[:100]
                    user = User(name=name, email=email, google_sub=sub)
                    user.set_password(None)
                    user.save()
            if not user.is_active:
                raise GoogleLoginError("account_inactive", "Esta conta está desativada.", 403)
            return user
    except IntegrityError as exc:
        raise GoogleLoginError("account_conflict", "Sua conta foi atualizada. Tente entrar novamente.", 409) from exc
