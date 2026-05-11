"""
Wipes ALL users (and their linked data) and creates a fresh admin account.
Run from the project root: python reset_admin_password.py
"""
from passlib.context import CryptContext
from backend.database import SessionLocal
from backend.models import Issue, RouteEvent, SavedRoute, User, Validation

pwd_context = CryptContext(schemes=['bcrypt'], deprecated='auto')

ADMIN_USERNAME = 'admin'
ADMIN_EMAIL    = 'admin@saferoute.com'
ADMIN_PASSWORD = 'Admin@2026'

db = SessionLocal()
try:
    # Delete in FK-safe order
    db.query(RouteEvent).delete()
    db.query(Validation).delete()
    db.query(SavedRoute).delete()
    db.query(Issue).delete()
    db.query(User).delete()
    db.commit()
    print("All users and related data cleared.")

    user = User(
        username=ADMIN_USERNAME,
        email=ADMIN_EMAIL,
        password_hash=pwd_context.hash(ADMIN_PASSWORD),
        reputation_score=1.5,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    print(f"Admin account created (id={user.id}).")
    print(f"  Username : {ADMIN_USERNAME}")
    print(f"  Password : {ADMIN_PASSWORD}")
finally:
    db.close()
