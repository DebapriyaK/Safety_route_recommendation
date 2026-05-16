import re
import socket
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import monotonic
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from backend.config import (
    ACCESS_TOKEN_EXPIRE_HOURS,
    ADMIN_USERNAMES,
    ALGORITHM,
    APP_URL,
    AUTH_LOGIN_MAX_PER_MINUTE,
    AUTH_REGISTER_MAX_PER_MINUTE,
    EMAIL_ENABLED,
    EMAIL_FROM,
    EMAIL_PASSWORD,
    SECRET_KEY,
)
from backend.email_utils import generate_verification_token, send_verification_email
from backend.database import get_db
from backend.models import Issue, User, Validation

router = APIRouter(prefix='/auth', tags=['auth'])

pwd_context = CryptContext(schemes=['bcrypt'], deprecated='auto')
security = HTTPBearer(auto_error=False)

_rate_windows = {
    'login': (AUTH_LOGIN_MAX_PER_MINUTE, 60.0),
    'register': (AUTH_REGISTER_MAX_PER_MINUTE, 60.0),
}
_rate_buckets: dict = defaultdict(deque)
_rate_lock = Lock()


class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = 'bearer'
    user: dict


_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_]+$')
_SPECIAL_CHARS = set('!@#$%^&*()_+-=[]{};\':"|,.<>/?`~')


def _email_domain_exists(email: str) -> bool:
    """Return False if the email's domain has no DNS records (domain doesn't exist)."""
    try:
        domain = email.rsplit('@', 1)[-1].lower().strip()
        socket.getaddrinfo(domain, None)
        return True
    except OSError:
        return False


def _is_admin(username: str) -> bool:
    return bool(ADMIN_USERNAMES) and username in ADMIN_USERNAMES


def _client_ip(request: Request) -> str:
    # Use the LAST valid IP in X-Forwarded-For — it is appended by the trusted
    # reverse proxy (Railway/nginx) and cannot be spoofed by the client.
    xff = request.headers.get('x-forwarded-for')
    if xff:
        for candidate in reversed(xff.split(',')):
            candidate = candidate.strip()
            try:
                socket.inet_aton(candidate)
                return candidate
            except OSError:
                pass
    if request.client and request.client.host:
        return request.client.host
    return 'unknown'


def _validate_password(pw: str) -> Optional[str]:
    """Return an error string if the password fails policy, else None."""
    if len(pw) < 8:
        return 'Password must be at least 8 characters.'
    if len(pw) > 72:
        return 'Password must be at most 72 characters.'
    if not any(c.isupper() for c in pw):
        return 'Password must contain at least one uppercase letter.'
    if not any(c.islower() for c in pw):
        return 'Password must contain at least one lowercase letter.'
    if not any(c.isdigit() for c in pw):
        return 'Password must contain at least one number.'
    if not any(c in _SPECIAL_CHARS for c in pw):
        return 'Password must contain at least one special character (!@#$%^&* etc.).'
    return None


def _enforce_rate_limit(request: Request, action: str) -> None:
    limit, window_sec = _rate_windows[action]
    now = monotonic()
    key = f"{action}:{_client_ip(request)}"

    with _rate_lock:
        bucket = _rate_buckets[key]
        cutoff = now - window_sec
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            retry_after = int(max(1, window_sec - (now - bucket[0])))
            raise HTTPException(
                status_code=429,
                detail=f'Too many {action} attempts. Please retry in {retry_after}s.',
                headers={'Retry-After': str(retry_after)},
            )

        bucket.append(now)


def create_access_token(user_id: int, username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {
        'sub': str(user_id),
        'username': username,
        'exp': expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired token',
            headers={'WWW-Authenticate': 'Bearer'},
        )


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Authentication required',
            headers={'WWW-Authenticate': 'Bearer'},
        )

    payload = _decode_token(credentials.credentials)
    user_id = int(payload.get('sub', 0))
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='User not found or deactivated',
            headers={'WWW-Authenticate': 'Bearer'},
        )
    return user


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> Optional[User]:
    if credentials is None:
        return None
    try:
        payload = _decode_token(credentials.credentials)
        user_id = int(payload.get('sub', 0))
        return db.query(User).filter(User.id == user_id, User.is_active == True).first()
    except HTTPException:
        return None


@router.post('/register', status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    _enforce_rate_limit(request, 'register')

    username = body.username.strip()
    if not (3 <= len(username) <= 20):
        raise HTTPException(status_code=400, detail='Username must be between 3 and 20 characters.')
    if not _USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail='Username can only contain letters, numbers, and underscores.')

    pw_error = _validate_password(body.password)
    if pw_error:
        raise HTTPException(status_code=400, detail=pw_error)

    if not _email_domain_exists(body.email):
        raise HTTPException(status_code=400, detail='Email domain does not exist. Please use a valid email address.')

    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail='Username already taken')
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=409, detail='Email already registered')

    if EMAIL_ENABLED:
        token = generate_verification_token()
        user = User(
            username=body.username,
            email=body.email,
            password_hash=pwd_context.hash(body.password),
            email_verified=False,
            verification_token=token,
        )
        db.add(user)
        db.commit()
        ok = send_verification_email(
            to_email=body.email,
            username=body.username,
            token=token,
            app_url=APP_URL,
            from_email=EMAIL_FROM,
            email_password=EMAIL_PASSWORD,
        )
        if not ok:
            db.delete(user)
            db.commit()
            raise HTTPException(
                status_code=503,
                detail='Could not send verification email. Please try again later.',
            )
        return {'message': 'Check your email to verify your account.', 'email': body.email}
    else:
        user = User(
            username=body.username,
            email=body.email,
            password_hash=pwd_context.hash(body.password),
            email_verified=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        access_token = create_access_token(user.id, user.username)
        return TokenResponse(
            access_token=access_token,
            user={
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'preferred_mode': user.preferred_mode,
                'reputation_score': user.reputation_score,
                'is_admin': _is_admin(user.username),
            },
        )



@router.get('/verify')
def verify_email(token: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.verification_token == token).first()
    if not user:
        return RedirectResponse(url='/login.html?email_error=invalid_or_expired_link')
    if user.email_verified:
        return RedirectResponse(url='/login.html?email_info=already_verified')
    user.email_verified = True
    user.verification_token = None
    db.commit()
    return RedirectResponse(url='/login.html?verified=1')


@router.post('/login', response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    _enforce_rate_limit(request, 'login')

    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not pwd_context.verify(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail='Invalid username or password')
    if not user.is_active:
        raise HTTPException(status_code=403, detail='Account deactivated')
    if EMAIL_ENABLED and not user.email_verified:
        raise HTTPException(status_code=403, detail='Please verify your email before logging in. Check your inbox.')

    token = create_access_token(user.id, user.username)
    return TokenResponse(
        access_token=token,
        user={
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'preferred_mode': user.preferred_mode,
            'reputation_score': user.reputation_score,
            'is_admin': _is_admin(user.username),
        },
    )


@router.get('/me')
def me(current_user: User = Depends(get_current_user)):
    return {
        'id': current_user.id,
        'username': current_user.username,
        'email': current_user.email,
        'created_at': current_user.created_at.isoformat() if current_user.created_at else None,
        'is_active': current_user.is_active,
        'preferred_mode': current_user.preferred_mode,
        'reputation_score': current_user.reputation_score,
        'is_admin': _is_admin(current_user.username),
    }


@router.patch('/profile/mode')
def update_preferred_mode(
    mode: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if mode not in ('walk', 'cycle', 'drive'):
        raise HTTPException(status_code=400, detail="mode must be 'walk', 'cycle', or 'drive'")
    current_user.preferred_mode = mode
    db.add(current_user)
    db.commit()
    return {'preferred_mode': mode}


@router.get('/profile/stats')
def profile_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    reported = db.query(Issue).filter(Issue.reporter_id == current_user.id).all()
    validations = db.query(Validation).filter(Validation.user_id == current_user.id).all()

    total_reported = len(reported)
    total_validated = len(validations)
    total_confirms = sum(1 for v in validations if v.response == 'confirm')
    total_dismissals = sum(1 for v in validations if v.response == 'dismiss')

    confirmed_reports = sum(1 for i in reported if i.num_confirmations > i.num_dismissals)
    dismissed_reports = sum(1 for i in reported if i.num_dismissals > i.num_confirmations)

    accuracy_rate = round((confirmed_reports / total_reported) * 100, 1) if total_reported else 0.0

    return {
        'user': {
            'id': current_user.id,
            'username': current_user.username,
            'email': current_user.email,
            'reputation_score': current_user.reputation_score,
        },
        'reported': {
            'total': total_reported,
            'currently_active': sum(1 for i in reported if i.is_active),
            'leaning_confirmed': confirmed_reports,
            'leaning_dismissed': dismissed_reports,
            'accuracy_rate': accuracy_rate,
        },
        'validations': {
            'total': total_validated,
            'confirm': total_confirms,
            'dismiss': total_dismissals,
        },
    }
