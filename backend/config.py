import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Load .env from backend/ regardless of launch location
load_dotenv(dotenv_path=Path(__file__).parent / '.env')


def _get_csv(name: str, default: str) -> List[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(',') if item.strip()]


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


DATABASE_URL: str = os.getenv('DATABASE_URL', 'postgresql://postgres:password@localhost:5432/saferoute')
SECRET_KEY: str = os.getenv('SECRET_KEY', 'change-me-in-production-use-long-random-string')
ALGORITHM: str = os.getenv('ALGORITHM', 'HS256')
ACCESS_TOKEN_EXPIRE_HOURS: int = _get_int('ACCESS_TOKEN_EXPIRE_HOURS', 24)

GEOAPIFY_KEY: str = os.getenv('GEOAPIFY_KEY', '')  # set via environment variable — never hardcode
OLA_MAPS_KEY: str = os.getenv('OLA_MAPS_KEY', '')
ORS_API_KEY: str = os.getenv('ORS_API_KEY', '')
DEFAULT_CITY_LAT: float = _get_float('DEFAULT_CITY_LAT', 12.9716)
DEFAULT_CITY_LON: float = _get_float('DEFAULT_CITY_LON', 77.5946)

# Security / deployment controls
CORS_ORIGINS: List[str] = _get_csv(
    'CORS_ORIGINS',
    'http://localhost:8000,http://127.0.0.1:8000',
)
ALLOW_ALL_CORS: bool = os.getenv('ALLOW_ALL_CORS', '0') == '1'

# Rate limiting controls
ROUTE_RATE_LIMIT: str = os.getenv('ROUTE_RATE_LIMIT', '20/minute')
AUTH_LOGIN_MAX_PER_MINUTE: int = _get_int('AUTH_LOGIN_MAX_PER_MINUTE', 5)
AUTH_REGISTER_MAX_PER_MINUTE: int = _get_int('AUTH_REGISTER_MAX_PER_MINUTE', 3)

# Background cleanup controls
ISSUE_CLEANUP_INTERVAL_MINUTES: int = _get_int('ISSUE_CLEANUP_INTERVAL_MINUTES', 30)
AUTO_CREATE_TABLES: bool = os.getenv('AUTO_CREATE_TABLES', '0') == '1'

# Routing performance / cache controls
ROUTING_SHORT_TRIP_UNSIMPLIFIED_MAX_M: float = _get_float('ROUTING_SHORT_TRIP_UNSIMPLIFIED_MAX_M', 2000.0)
ROUTING_GRAPH_MAX_DIST_M: int = _get_int('ROUTING_GRAPH_MAX_DIST_M', 8000)
ROUTING_GRAPH_MIN_DIST_M: int = _get_int('ROUTING_GRAPH_MIN_DIST_M', 1500)
GRAPH_CACHE_MAX_ITEMS: int = _get_int('GRAPH_CACHE_MAX_ITEMS', 20)
GRAPH_CACHE_TTL_MINUTES: int = _get_int('GRAPH_CACHE_TTL_MINUTES', 90)
ROUTING_CACHE_CENTER_DECIMALS: int = _get_int('ROUTING_CACHE_CENTER_DECIMALS', 1)
ROUTING_PRELOAD_ENABLED: bool = os.getenv('ROUTING_PRELOAD_ENABLED', '1') == '1'
ROUTING_PRELOAD_BLOCKING: bool = os.getenv('ROUTING_PRELOAD_BLOCKING', '0') == '1'
ROUTING_PRELOAD_DIST_M: int = _get_int('ROUTING_PRELOAD_DIST_M', 10000)
ROUTING_PRELOAD_MODES: List[str] = _get_csv('ROUTING_PRELOAD_MODES', 'walk,cycle,drive')


ADMIN_USERNAMES: List[str] = _get_csv('ADMIN_USERNAMES', '')

# Email verification (Gmail SMTP)
EMAIL_ENABLED: bool = os.getenv('EMAIL_ENABLED', '0') == '1'
EMAIL_FROM: str = os.getenv('EMAIL_FROM', '')
EMAIL_API_KEY: str = os.getenv('EMAIL_API_KEY', '')
APP_URL: str = os.getenv('APP_URL', 'http://localhost:8000')


def validate_runtime_config() -> List[str]:
    warnings: List[str] = []
    if SECRET_KEY.startswith('change-me') or len(SECRET_KEY) < 24:
        warnings.append('SECRET_KEY is weak/default. Use a long random secret in production.')
    if ACCESS_TOKEN_EXPIRE_HOURS > 168:
        warnings.append('ACCESS_TOKEN_EXPIRE_HOURS is very high; consider shorter-lived tokens.')
    if ALLOW_ALL_CORS:
        warnings.append('ALLOW_ALL_CORS is enabled; this is unsafe for production.')
    return warnings
