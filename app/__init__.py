from flask import Flask

from app.config import Config
from app.extensions import login_manager, db, migrate
from app.web import blueprint as web_blueprint
from app.models import User


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    app.register_blueprint(web_blueprint)
    return app