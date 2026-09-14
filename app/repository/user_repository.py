from app.extensions import db
from app.models import User


class UserRepository:
    def get_by_id(self, id):
        return db.session.query(User).filter(User.id == id).first()

    def get_by_username(self, username):
        return db.session.query(User).filter(User.username == username).first()

    def get_by_email(self, email):
        return db.session.query(User).filter(User.email == email).first()

    def get_all(self):
        return db.session.query(User).all()

    def save(self, user):
        db.session.add(user)
        db.session.flush()
        return user

    def delete(self, user):
        db.session.delete(user)
        db.session.commit()

    def commit(self):
        db.session.commit()