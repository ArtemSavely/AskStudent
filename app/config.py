import secrets


class Config:
    SECRET_KEY = secrets.token_urlsafe(32)
    SQLALCHEMY_DATABASE_URI = "sqlite:///app.db"