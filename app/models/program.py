from app import db


class Program(db.Model):
    __tablename__ = "programs"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"),
                              nullable=False, index=True)

    university = db.relationship("University", back_populates="programs")