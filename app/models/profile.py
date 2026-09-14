from app import db


class Profile(db.Model):
    __tablename__ = "profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        unique=True, nullable=False)

    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100))
    city = db.Column(db.String(100))

    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"))
    program_id = db.Column(db.Integer, db.ForeignKey("programs.id"))
    course = db.Column(db.SmallInteger)

    user = db.relationship("User", back_populates="profile")