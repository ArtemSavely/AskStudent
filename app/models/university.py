from app import db


class University(db.Model):
    __tablename__ = "universities"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    city = db.Column(db.String(100), index=True)

    programs = db.relationship("Program", back_populates="university")