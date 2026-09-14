from app.models import User
from werkzeug.security import generate_password_hash, check_password_hash


class UserService:
    def create_user(self, email, password, username, role):
        if self.user_repo.get_by_email(email):
            raise ValueError("Пользователь с этой почтой уже существует")
        if self.user_repo.get_by_username(username):
            raise ValueError("Пользователь с таким именем уже существует")

        user = User(email=email,
                    username=username,
                    hashed_password=generate_password_hash(password),
                    role=role)
        user = self.user_repo.save(user)
        self.user_repo.commit()
        return user

    def authenticate(self, email, password):
        user = self.user_repo.get_by_email(email)
        if user and check_password_hash(user.hashed_password, password):
            return user
        return

    def get_user_by_name(self, username):
        return self.user_repo.get_by_username(username)

    def get_all(self):
        return self.user_repo.get_all()

    def get_user_by_id(self, user_id):
        return self.user_repo.get_by_id(user_id)

    def get_user_by_email(self, email):
        return self.user_repo.get_by_email(email)