from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import monotonic
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from backend.config import (
    ACCESS_TOKEN_EXPIRE_HOURS,
    ALGORITHM,
    AUTH_LOGIN_MAX_PER_MINUTE,
    AUTH_REGISTER_MAX_PER_MINUTE,
    SECRET_KEY,
)
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


def _client_ip(request: Request) -> str:
    xff = request.headers.get('x-forwarded-for')
    if xff:
        return xff.split(',')[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return 'unknown'


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


@router.post('/register', response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    _enforce_rate_limit(request, 'register')

    if len(body.username) < 3:
        raise HTTPException(status_code=400, detail='Username must be at least 3 characters')
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail='Password must be at least 6 characters')

    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail='Username already taken')
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=409, detail='Email already registered')

    user = User(
        username=body.username,
        email=body.email,
        password_hash=pwd_context.hash(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id, user.username)
    return TokenResponse(
        access_token=token,
        user={
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'preferred_mode': user.preferred_mode,
            'reputation_score': user.reputation_score,
        },
    )


@router.post('/login', response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    _enforce_rate_limit(request, 'login')

    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not pwd_context.verify(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail='Invalid username or password')
    if not user.is_active:
        raise HTTPException(status_code=403, detail='Account deactivated')

    token = create_access_token(user.id, user.username)
    return TokenResponse(
        access_token=token,
        user={
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'preferred_mode': user.preferred_mode,
            'reputation_score': user.reputation_score,
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
