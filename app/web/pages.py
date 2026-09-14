from flask import Blueprint, redirect, render_template, request, abort, url_for
from flask_login import login_user, logout_user, login_required, current_user


blueprint = Blueprint('web', __name__)
