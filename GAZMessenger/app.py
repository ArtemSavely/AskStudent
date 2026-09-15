from gevent import monkey
monkey.patch_all()

import ssl
ssl._create_default_https_context = ssl._create_unverified_context

import os
import re
import uuid
import logging
import mimetypes
import time
import urllib.request
import feedparser
from functools import wraps
from datetime import datetime, timezone, timedelta
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_from_directory, abort, jsonify
)
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user
)
from PIL import Image
from sqlalchemy import or_, and_, desc

from models import (
    db, User, Message, Room, DirectMessage, RoomMember, RoomBan,
    Friend, FriendRequest, Subscription, Notification, NotificationSetting,
    Post, PostLike, PostComment, PostView, MessageReaction,
    CommentLike, CommentReply, MSK, now_msk
)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'gaz-messenger-by-sergej-fedorkin'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///chat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

TELEGRAM_URL = 'https://t.me/herrneincameraden'
AUTHOR_NAME = 'Sergej Fedorkin'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
AVATAR_DIR = os.path.join(UPLOAD_DIR, 'avatars')
FILES_DIR = os.path.join(UPLOAD_DIR, 'files')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(AVATAR_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)

IMAGE_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'}
MAX_SIZE = 50 * 1024 * 1024
app.config['MAX_CONTENT_LENGTH'] = MAX_SIZE

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

NEWS_FEEDS = [
    {'name': 'Народное образование', 'url': 'https://nokid.ru/feed/', 'lang': 'ru', 'category': 'russia'},
    {'name': 'Учительская газета', 'url': 'https://ug.ru/feed/', 'lang': 'ru', 'category': 'russia'},
    {'name': 'Педсовет', 'url': 'https://pedsovet.org/rss', 'lang': 'ru', 'category': 'russia'},
    {'name': 'Inside Higher Ed', 'url': 'https://www.insidehighered.com/rss.xml', 'lang': 'en', 'category': 'world'},
    {'name': 'The Conversation · Education', 'url': 'https://theconversation.com/articles.atom?section=education', 'lang': 'en', 'category': 'world'},
    {'name': 'EdSurge', 'url': 'https://www.edsurge.com/articles_rss', 'lang': 'en', 'category': 'world'},
    {'name': 'The Guardian · Education', 'url': 'https://www.theguardian.com/education/rss', 'lang': 'en', 'category': 'world'},
    {'name': 'ScienceDaily · Education', 'url': 'https://www.sciencedaily.com/rss/education_learning.xml', 'lang': 'en', 'category': 'world'},
]

CATEGORY_ORDER = {'russia': 0, 'admission': 1, 'world': 2}

_news_cache = {'data': [], 'ts': 0}
NEWS_CACHE_TTL = 600
RSS_TIMEOUT = 15

REACTION_EMOJIS = ['👍', '❤️', '😂', '🔥', '😮', '😢', '🎉', '👏']

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'

socketio = SocketIO(app, cors_allowed_origins="*")

online_users = {}
typing_users = {}

logging.getLogger('engineio').setLevel(logging.WARNING)
logging.getLogger('socketio').setLevel(logging.WARNING)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Только для администратора')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return wrapper


def get_ext(filename):
    return filename.rsplit('.', 1)[1].lower() if '.' in filename else ''


def is_image(filename):
    return get_ext(filename) in IMAGE_EXT


def save_image(file, max_size=1280, quality=85):
    ext = get_ext(file.filename)
    fname = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(UPLOAD_DIR, fname)
    img = Image.open(file.stream)
    if img.mode in ('RGBA', 'P') and ext in ('jpg', 'jpeg'):
        img = img.convert('RGB')
    img.thumbnail((max_size, max_size))
    img.save(path, optimize=True, quality=quality)
    return fname


def save_avatar(file):
    ext = get_ext(file.filename)
    fname = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(AVATAR_DIR, fname)
    img = Image.open(file.stream)
    if img.mode in ('RGBA', 'P') and ext in ('jpg', 'jpeg'):
        img = img.convert('RGB')
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((256, 256), Image.LANCZOS)
    img.save(path, optimize=True, quality=90)
    return fname


def save_file(file):
    original = file.filename or 'file'
    ext = get_ext(original)
    fname = f"{uuid.uuid4().hex}" + (f".{ext}" if ext else "")
    path = os.path.join(FILES_DIR, fname)
    file.save(path)
    mime = file.mimetype or mimetypes.guess_type(original)[0] or 'application/octet-stream'
    return fname, original, mime


def human_size(n):
    for unit in ('B', 'КБ', 'МБ', 'ГБ'):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def now_str():
    return datetime.now(MSK).strftime('%H:%M:%S')


def time_ago(dt):
    if not dt:
        return ''
    delta = now_msk() - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return 'только что'
    if seconds < 3600:
        return f'{seconds // 60} мин. назад'
    if seconds < 86400:
        return f'{seconds // 3600} ч. назад'
    if seconds < 604800:
        return f'{seconds // 86400} дн. назад'
    return dt.strftime('%d.%m.%Y %H:%M')


@app.context_processor
def inject_globals():
    ctx = {
        'TELEGRAM_URL': TELEGRAM_URL,
        'AUTHOR_NAME': AUTHOR_NAME,
        'time_ago': time_ago,
        'REACTION_EMOJIS': REACTION_EMOJIS,
        'sound_settings': {
            'enabled': True,
            'volume': 70,
            'dm': True,
            'group': True,
            'notify': True,
        }
    }
    if current_user.is_authenticated:
        try:
            ctx['unread_notifications'] = current_user.unread_notifications_count()
        except Exception:
            ctx['unread_notifications'] = 0
        try:
            ctx['friend_requests'] = current_user.incoming_count()
        except Exception:
            ctx['friend_requests'] = 0
        try:
            s = current_user.get_settings()
            ctx['sound_settings'] = {
                'enabled': bool(s.sound_enabled),
                'volume': int(s.sound_volume or 70),
                'dm': bool(s.sound_dm),
                'group': bool(s.sound_group),
                'notify': bool(s.sound_notify),
            }
        except Exception:
            pass
    else:
        ctx['unread_notifications'] = 0
        ctx['friend_requests'] = 0
    return ctx


def _user_wants(user_id, kind):
    s = NotificationSetting.query.filter_by(user_id=user_id).first()
    if not s:
        s = NotificationSetting(user_id=user_id)
        db.session.add(s)
        db.session.commit()
    mapping = {
        'dm': s.notify_dm,
        'group_msg': s.notify_group,
        'like_post': s.notify_like,
        'comment_post': s.notify_comment,
        'follow': s.notify_follow,
        'friend_request': s.notify_friend,
        'friend_accept': s.notify_friend,
        'new_post': s.notify_new_post,
        'world_news': s.notify_world_news,
        'reaction': s.notify_like,
    }
    return mapping.get(kind, True)


def notify(user_id, actor_id, type_, text, link=None):
    if not user_id or user_id == actor_id:
        return
    if not _user_wants(user_id, type_):
        return
    n = Notification(user_id=user_id, actor_id=actor_id, type=type_, text=text, link=link)
    db.session.add(n)
    db.session.commit()
    for sid, u in online_users.items():
        if u['id'] == user_id:
            unread = Notification.query.filter_by(user_id=user_id, is_read=False).count()
            socketio.emit('notification_new', {
                'id': n.id,
                'type': type_,
                'text': text,
                'link': link,
                'actor_id': actor_id,
                'unread': unread,
                'time': time_ago(n.created_at)
            }, to=sid)


def reactions_for_message(message_id, user_id):
    rows = MessageReaction.query.filter_by(message_id=message_id).all()
    grouped = {}
    for r in rows:
        grouped.setdefault(r.emoji, {'count': 0, 'users': [], 'mine': False})
        grouped[r.emoji]['count'] += 1
        grouped[r.emoji]['users'].append(r.user.username)
        if r.user_id == user_id:
            grouped[r.emoji]['mine'] = True
    return grouped


def reply_preview_for_message(msg):
    if not msg or not msg.reply_to_id:
        return None
    parent = db.session.get(Message, msg.reply_to_id)
    if not parent:
        return None
    text = (parent.text or '')[:120] if parent.text else ('📷 фото' if parent.image else ('🎤 голосовое' if parent.file_type and 'audio' in parent.file_type else '📎 файл'))
    return {
        'id': parent.id,
        'username': parent.author.username,
        'text': text,
    }


def reply_preview_for_dm(msg):
    if not msg or not msg.reply_to_id:
        return None
    parent = db.session.get(DirectMessage, msg.reply_to_id)
    if not parent:
        return None
    text = (parent.text or '')[:120] if parent.text else ('📷 фото' if parent.image else ('🎤 голосовое' if parent.file_type and 'audio' in parent.file_type else '📎 файл'))
    return {
        'id': parent.id,
        'username': parent.sender.username,
        'text': text,
    }


def serialize_message(m, user=None):
    rx = reactions_for_message(m.id, user.id if user else None)
    return {
        'id': m.id,
        'username': m.author.username,
        'user_id': m.user_id,
        'avatar': m.author.avatar_url,
        'is_admin': bool(m.author.is_admin),
        'msg': m.text or '',
        'image': m.image,
        'file': m.file,
        'file_name': m.file_name,
        'file_type': m.file_type,
        'time': m.created_at.strftime('%H:%M:%S'),
        'edited': bool(m.edited_at),
        'reactions': rx,
        'reply_to': reply_preview_for_message(m),
    }


def serialize_dm(m, user=None):
    rx = reactions_for_message(m.id, user.id if user else None)
    return {
        'id': m.id,
        'sender_id': m.sender_id,
        'recipient_id': m.recipient_id,
        'username': m.sender.username,
        'avatar': m.sender.avatar_url,
        'is_admin': bool(m.sender.is_admin),
        'msg': m.text or '',
        'image': m.image,
        'file': m.file,
        'file_name': m.file_name,
        'file_type': m.file_type,
        'time': m.created_at.strftime('%H:%M:%S'),
        'is_read': m.is_read,
        'edited': bool(m.edited_at),
        'reactions': rx,
        'reply_to': reply_preview_for_dm(m),
    }


def serialize_post(p, user):
    return {
        'id': p.id,
        'user_id': p.user_id,
        'username': p.author.username,
        'avatar': p.author.avatar_url,
        'is_admin': bool(p.author.is_admin),
        'text': p.text or '',
        'image': p.image,
        'file': p.file,
        'file_name': p.file_name,
        'file_type': p.file_type,
        'views': p.views,
        'likes': p.likes_count(),
        'comments': p.comments_count(),
        'liked': p.is_liked_by(user.id) if user and user.is_authenticated else False,
        'time': time_ago(p.created_at),
        'created_at': p.created_at.strftime('%d.%m.%Y %H:%M'),
        'edited': bool(p.edited_at),
    }


def users_in_room(room_id):
    result = []
    seen = set()
    for u in online_users.values():
        if u.get('room') == room_id and u['id'] not in seen:
            seen.add(u['id'])
            user = db.session.get(User, u['id'])
            result.append({
                'id': u['id'],
                'username': u['username'],
                'avatar': user.avatar_url if user else None,
                'is_admin': bool(user.is_admin) if user else False
            })
    result.sort(key=lambda x: x['username'].lower())
    return result


def room_members(room_id):
    members = RoomMember.query.filter_by(room_id=room_id).all()
    result = []
    for m in members:
        u = m.user
        if u:
            result.append({
                'id': u.id,
                'username': u.username,
                'avatar': u.avatar_url,
                'is_admin': bool(u.is_admin)
            })
    result.sort(key=lambda x: x['username'].lower())
    return result


def is_room_member(room_id, user_id):
    return RoomMember.query.filter_by(room_id=room_id, user_id=user_id).first() is not None


def is_room_banned(room_id, user_id):
    return RoomBan.query.filter_by(room_id=room_id, user_id=user_id).first() is not None


def ensure_member(room_id, user_id):
    if not is_room_member(room_id, user_id):
        db.session.add(RoomMember(room_id=room_id, user_id=user_id))
        db.session.commit()


def rooms_visible_to(user):
    if user.is_admin:
        return Room.query.order_by(Room.name).all()
    member_ids = {m.room_id for m in RoomMember.query.filter_by(user_id=user.id).all()}
    general = Room.query.filter_by(name='Общий').first()
    if general:
        member_ids.add(general.id)
    if not member_ids:
        return []
    return (Room.query.filter(Room.id.in_(member_ids)).order_by(Room.name).all())


def dm_room_name(user_a, user_b):
    a, b = sorted([user_a, user_b])
    return f'dm_{a}_{b}'


def dm_room_names_for(user_id):
    ids = db.session.query(DirectMessage.sender_id).filter_by(recipient_id=user_id)
    ids2 = db.session.query(DirectMessage.recipient_id).filter_by(sender_id=user_id)
    others = {row[0] for row in ids} | {row[0] for row in ids2}
    return [dm_room_name(user_id, other) for other in others]


def find_user_by_login(login):
    login = login.strip()
    if '@' in login:
        return User.query.filter_by(email=login.lower()).first()
    return User.query.filter_by(username=login).first()


def register_view(post, user):
    if not user or not user.is_authenticated:
        return
    if post.user_id == user.id:
        return
    exists = PostView.query.filter_by(post_id=post.id, user_id=user.id).first()
    if not exists:
        db.session.add(PostView(post_id=post.id, user_id=user.id))
        post.views = (post.views or 0) + 1
        db.session.commit()


def _parse_rss_date(s):
    if not s:
        return 0
    for fmt in (
        '%a, %d %b %Y %H:%M:%S %z',
        '%a, %d %b %Y %H:%M:%S %Z',
        '%a, %d %b %Y %H:%M:%S',
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%dT%H:%M:%SZ',
        '%Y-%m-%d %H:%M:%S',
    ):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except Exception:
            continue
    return 0


def _fetch_one_feed(feed):
    result = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
        'Connection': 'keep-alive',
    }
    parsed = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(feed['url'], headers=headers)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=RSS_TIMEOUT, context=ctx) as resp:
                raw = resp.read()
            parsed = feedparser.parse(raw)
            break
        except Exception as e:
            if attempt == 0:
                time.sleep(1)
                continue
            logging.info(f'RSS skip {feed["name"]}: {type(e).__name__}')
            return []
    if not parsed:
        return []
    for entry in parsed.entries[:20]:
        title = getattr(entry, 'title', '').strip()
        link = getattr(entry, 'link', '').strip()
        if not title or not link:
            continue
        summary = getattr(entry, 'summary', '') or ''
        summary = re.sub(r'<[^>]+>', '', summary)[:280].strip()
        published = getattr(entry, 'published', '') or getattr(entry, 'updated', '') or ''
        result.append({
            'source': feed['name'],
            'title': title,
            'link': link,
            'summary': summary,
            'published': published,
            'lang': feed['lang'],
            'category': feed.get('category', 'world'),
            '_ts': _parse_rss_date(published),
        })
    return result


def fetch_news(force=False):
    now = time.time()
    if not force and _news_cache['data'] and (now - _news_cache['ts'] < NEWS_CACHE_TTL):
        return _news_cache['data']
    items = []
    seen_links = set()
    try:
        import gevent
        jobs = [gevent.spawn(_fetch_one_feed, feed) for feed in NEWS_FEEDS]
        gevent.joinall(jobs, timeout=RSS_TIMEOUT + 5)
        for job in jobs:
            try:
                batch = job.value or []
            except Exception:
                batch = []
            for it in batch:
                if it['link'] in seen_links:
                    continue
                seen_links.add(it['link'])
                items.append(it)
    except Exception as e:
        logging.warning(f'gevent parallel fetch failed: {e}')
        for feed in NEWS_FEEDS:
            for it in _fetch_one_feed(feed):
                if it['link'] in seen_links:
                    continue
                seen_links.add(it['link'])
                items.append(it)
    items.sort(key=lambda x: (CATEGORY_ORDER.get(x['category'], 99), -x['_ts']))
    for it in items:
        it.pop('_ts', None)
    _news_cache['data'] = items[:120]
    _news_cache['ts'] = now
    return _news_cache['data']


@app.route('/')
@login_required
def index():
    general = Room.query.filter_by(name='Общий').first()
    if not general:
        general = Room(name='Общий')
        db.session.add(general)
        db.session.commit()
        ensure_member(general.id, current_user.id)
    return redirect(url_for('room_view', room_id=general.id))


@app.route('/search')
@login_required
def search_page():
    q = request.args.get('q', '').strip()
    scope = request.args.get('scope', 'all')
    results = []
    if q and len(q) >= 2:
        pattern = f'%{q}%'
        if scope in ('all', 'rooms'):
            room_msgs = (Message.query
                         .filter(Message.text.isnot(None))
                         .filter(Message.text.ilike(pattern))
                         .order_by(desc(Message.created_at))
                         .limit(200).all())
            for m in room_msgs:
                room = db.session.get(Room, m.room_id)
                if room:
                    if not current_user.is_admin:
                        is_public = (room.name == 'Общий')
                        is_member = is_room_member(room.id, current_user.id)
                        if not (is_public or is_member):
                            continue
                    results.append({
                        'kind': 'room',
                        'message': m,
                        'room': room,
                        'snippet': m.text,
                        'time': m.created_at,
                    })
        if scope in ('all', 'dm'):
            friend_ids = current_user.friend_ids()
            if friend_ids:
                dm_msgs = (DirectMessage.query
                           .filter(DirectMessage.text.isnot(None))
                           .filter(DirectMessage.text.ilike(pattern))
                           .filter(or_(
                               and_(DirectMessage.sender_id == current_user.id,
                                    DirectMessage.recipient_id.in_(friend_ids)),
                               and_(DirectMessage.recipient_id == current_user.id,
                                    DirectMessage.sender_id.in_(friend_ids)),
                           ))
                           .order_by(desc(DirectMessage.created_at))
                           .limit(200).all())
                for m in dm_msgs:
                    partner_id = m.recipient_id if m.sender_id == current_user.id else m.sender_id
                    partner = db.session.get(User, partner_id)
                    if partner:
                        results.append({
                            'kind': 'dm',
                            'message': m,
                            'partner': partner,
                            'snippet': m.text,
                            'time': m.created_at,
                        })
        results.sort(key=lambda x: x['time'], reverse=True)
        results = results[:200]
    return render_template('search.html', q=q, scope=scope, results=results)


@app.route('/students')
@login_required
def students_search():
    q_univ = request.args.get('university', '').strip()
    q_faculty = request.args.get('faculty', '').strip()
    q_course = request.args.get('course', '').strip()

    query = User.query.filter(
        User.university.isnot(None),
        User.is_banned == False,
    )

    if q_univ:
        query = query.filter(User.university.ilike(f'%{q_univ}%'))
    if q_faculty:
        query = query.filter(User.faculty.ilike(f'%{q_faculty}%'))
    if q_course:
        query = query.filter(User.course.ilike(f'%{q_course}%'))

    results = query.order_by(User.university, User.faculty, User.course, User.username).limit(200).all()

    universities = [
        row[0] for row in db.session.query(User.university)
        .filter(User.university.isnot(None))
        .distinct()
        .order_by(User.university)
        .all()
    ]
    faculties = [
        row[0] for row in db.session.query(User.faculty)
        .filter(User.faculty.isnot(None))
        .distinct()
        .order_by(User.faculty)
        .all()
    ]
    courses = [
        row[0] for row in db.session.query(User.course)
        .filter(User.course.isnot(None))
        .distinct()
        .order_by(User.course)
        .all()
    ]

    return render_template(
        'students.html',
        results=results,
        q_univ=q_univ,
        q_faculty=q_faculty,
        q_course=q_course,
        universities=universities,
        faculties=faculties,
        courses=courses,
    )


@app.route('/notifications')
@login_required
def notifications_list():
    items = current_user.notifications()
    return render_template('notifications.html', items=items)


@app.route('/notifications/read/<int:nid>', methods=['POST'])
@login_required
def notification_read(nid):
    n = db.session.get(Notification, nid)
    if not n or n.user_id != current_user.id:
        abort(404)
    n.is_read = True
    db.session.commit()
    if n.link:
        return redirect(n.link)
    return redirect(url_for('notifications_list'))


@app.route('/notifications/read-all', methods=['POST'])
@login_required
def notifications_read_all():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    flash('Все уведомления прочитаны')
    return redirect(url_for('notifications_list'))


@app.route('/notifications/clear', methods=['POST'])
@login_required
def notifications_clear():
    Notification.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    flash('Уведомления очищены')
    return redirect(url_for('notifications_list'))


@app.route('/notifications/settings', methods=['GET', 'POST'])
@login_required
def notification_settings():
    s = current_user.get_settings()
    if request.method == 'POST':
        s.notify_dm = bool(request.form.get('notify_dm'))
        s.notify_group = bool(request.form.get('notify_group'))
        s.notify_like = bool(request.form.get('notify_like'))
        s.notify_comment = bool(request.form.get('notify_comment'))
        s.notify_follow = bool(request.form.get('notify_follow'))
        s.notify_friend = bool(request.form.get('notify_friend'))
        s.notify_new_post = bool(request.form.get('notify_new_post'))
        s.notify_world_news = bool(request.form.get('notify_world_news'))
        db.session.commit()
        flash('Настройки сохранены')
        return redirect(url_for('notification_settings'))
    return render_template('notification_settings.html', s=s)


@app.route('/news')
@login_required
def news_page():
    force = request.args.get('force') == '1'
    try:
        items = fetch_news(force=force)
    except Exception as e:
        logging.error(f'news fetch: {e}')
        items = []
    return render_template('news.html', items=items)


@app.route('/followers/<int:user_id>')
@login_required
def followers_page(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    followers = user.followers()
    following = user.following()
    return render_template(
        'followers.html',
        wall_user=user,
        followers=followers,
        following=following,
        is_self=(user.id == current_user.id)
    )


@app.route('/feed')
@login_required
def feed():
    friend_ids = current_user.friend_ids()
    friend_posts = (Post.query
                    .filter(Post.user_id.in_(friend_ids))
                    .order_by(desc(Post.created_at))
                    .limit(100).all()) if friend_ids else []
    exclude_ids = friend_ids | {current_user.id}
    other_posts = (Post.query
                   .filter(~Post.user_id.in_(exclude_ids))
                   .order_by(desc(Post.created_at))
                   .limit(100).all())
    my_posts = (Post.query
                .filter_by(user_id=current_user.id)
                .order_by(desc(Post.created_at))
                .limit(50).all())
    posts = (friend_posts + my_posts + other_posts)[:200]
    return render_template('feed.html', posts=posts)


@app.route('/wall/<int:user_id>')
@login_required
def wall(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    posts = (Post.query
             .filter_by(user_id=user.id)
             .order_by(desc(Post.created_at))
             .all())
    is_self = (user.id == current_user.id)
    is_following = current_user.is_following(user.id) if not is_self else False
    is_friend = current_user.is_friend_with(user.id) if not is_self else False
    return render_template(
        'wall.html',
        wall_user=user,
        posts=posts,
        is_self=is_self,
        is_following=is_following,
        is_friend=is_friend
    )


@app.route('/post/<int:post_id>')
@login_required
def post_view(post_id):
    post = db.session.get(Post, post_id)
    if not post:
        abort(404)
    register_view(post, current_user)
    comments = (PostComment.query
                .filter_by(post_id=post.id)
                .order_by(PostComment.created_at)
                .all())
    return render_template('post.html', post=post, comments=comments)


@app.route('/post/create', methods=['POST'])
@login_required
def post_create():
    text = request.form.get('text', '').strip()
    image = request.form.get('image') or None
    file = request.form.get('file') or None
    file_name = request.form.get('file_name') or None
    file_type = request.form.get('file_type') or None
    if not text and not image and not file:
        flash('Пост пуст')
        return redirect(request.referrer or url_for('feed'))
    if len(text) > 5000:
        flash('Слишком длинный пост')
        return redirect(request.referrer or url_for('feed'))
    post = Post(
        user_id=current_user.id,
        text=text or None,
        image=image,
        file=file,
        file_name=file_name,
        file_type=file_type
    )
    db.session.add(post)
    db.session.commit()
    try:
        subs = Subscription.query.filter_by(following_id=current_user.id).all()
        for s in subs:
            notify(
                s.follower_id, current_user.id, 'new_post',
                f'📰 {current_user.username} опубликовал новый пост',
                url_for('post_view', post_id=post.id)
            )
    except Exception as e:
        logging.warning(f'notify new_post: {e}')
    socketio.emit('new_post', serialize_post(post, current_user))
    flash('Пост опубликован')
    return redirect(request.referrer or url_for('wall', user_id=current_user.id))


@app.route('/post/<int:post_id>/delete', methods=['POST'])
@login_required
def post_delete(post_id):
    post = db.session.get(Post, post_id)
    if not post:
        abort(404)
    if not current_user.is_admin and post.user_id != current_user.id:
        flash('Можно удалять только свои посты')
        return redirect(url_for('feed'))
    if post.image:
        try: os.remove(os.path.join(UPLOAD_DIR, post.image))
        except OSError: pass
    if post.file:
        try: os.remove(os.path.join(FILES_DIR, post.file))
        except OSError: pass
    pid = post.id
    db.session.delete(post)
    db.session.commit()
    socketio.emit('post_deleted', {'id': pid})
    flash('Пост удалён')
    return redirect(request.referrer or url_for('feed'))


@app.route('/post/<int:post_id>/edit', methods=['POST'])
@login_required
def post_edit(post_id):
    post = db.session.get(Post, post_id)
    if not post:
        abort(404)
    if not current_user.is_admin and post.user_id != current_user.id:
        flash('Можно редактировать только свои посты')
        return redirect(url_for('post_view', post_id=post_id))
    new_text = request.form.get('text', '').strip()
    if not new_text:
        flash('Пустой текст')
        return redirect(url_for('post_view', post_id=post_id))
    if len(new_text) > 5000:
        flash('Слишком длинный текст')
        return redirect(url_for('post_view', post_id=post_id))
    post.text = new_text
    post.edited_at = now_msk()
    db.session.commit()
    socketio.emit('post_edited', {'id': post.id, 'text': post.text})
    flash('Пост обновлён')
    return redirect(url_for('post_view', post_id=post_id))


@app.route('/post/<int:post_id>/like', methods=['POST'])
@login_required
def post_like(post_id):
    post = db.session.get(Post, post_id)
    if not post:
        return jsonify({'error': 'not found'}), 404
    existing = PostLike.query.filter_by(post_id=post.id, user_id=current_user.id).first()
    if existing:
        db.session.delete(existing)
        liked = False
    else:
        db.session.add(PostLike(post_id=post.id, user_id=current_user.id))
        liked = True
        notify(
            post.user_id, current_user.id, 'like_post',
            f'❤️ {current_user.username} лайкнул ваш пост',
            url_for('post_view', post_id=post.id)
        )
    db.session.commit()
    likes = post.likes_count()
    socketio.emit('post_like_update', {'post_id': post.id, 'likes': likes})
    return jsonify({'liked': liked, 'likes': likes})


@app.route('/post/<int:post_id>/comment', methods=['POST'])
@login_required
def post_comment(post_id):
    post = db.session.get(Post, post_id)
    if not post:
        abort(404)
    text = request.form.get('text', '').strip()
    if not text:
        flash('Пустой комментарий')
        return redirect(url_for('post_view', post_id=post_id))
    if len(text) > 1000:
        flash('Слишком длинный комментарий')
        return redirect(url_for('post_view', post_id=post_id))
    c = PostComment(post_id=post.id, user_id=current_user.id, text=text)
    db.session.add(c)
    db.session.commit()
    snippet = text[:80] + ('…' if len(text) > 80 else '')
    notify(
        post.user_id, current_user.id, 'comment_post',
        f'💬 {current_user.username}: {snippet}',
        url_for('post_view', post_id=post.id)
    )
    socketio.emit('post_comment_new', {
        'post_id': post.id,
        'comments': post.comments_count(),
        'comment': {
            'id': c.id,
            'username': c.author.username,
            'avatar': c.author.avatar_url,
            'is_admin': bool(c.author.is_admin),
            'text': c.text,
            'time': time_ago(c.created_at)
        }
    })
    return redirect(url_for('post_view', post_id=post_id))


@app.route('/comment/<int:comment_id>/delete', methods=['POST'])
@login_required
def comment_delete(comment_id):
    c = db.session.get(PostComment, comment_id)
    if not c:
        abort(404)
    if not current_user.is_admin and c.user_id != current_user.id:
        flash('Можно удалять только свои комментарии')
        return redirect(url_for('post_view', post_id=c.post_id))
    pid = c.post_id
    cid = c.id
    CommentReply.query.filter_by(comment_id=cid).delete()
    CommentLike.query.filter_by(comment_id=cid).delete()
    db.session.delete(c)
    db.session.commit()
    socketio.emit('post_comment_deleted', {'comment_id': cid, 'post_id': pid})
    flash('Комментарий удалён')
    return redirect(url_for('post_view', post_id=pid))


@app.route('/comment/<int:comment_id>/like', methods=['POST'])
@login_required
def comment_like(comment_id):
    c = db.session.get(PostComment, comment_id)
    if not c:
        return jsonify({'error': 'not found'}), 404
    existing = CommentLike.query.filter_by(comment_id=c.id, user_id=current_user.id).first()
    if existing:
        db.session.delete(existing)
        liked = False
    else:
        db.session.add(CommentLike(comment_id=c.id, user_id=current_user.id))
        liked = True
        if c.user_id != current_user.id:
            notify(
                c.user_id, current_user.id, 'like_post',
                f'❤️ {current_user.username} лайкнул ваш комментарий',
                url_for('post_view', post_id=c.post_id)
            )
    db.session.commit()
    likes = c.likes_count()
    return jsonify({'liked': liked, 'likes': likes})


@app.route('/comment/<int:comment_id>/reply', methods=['POST'])
@login_required
def comment_reply(comment_id):
    c = db.session.get(PostComment, comment_id)
    if not c:
        abort(404)
    text = request.form.get('text', '').strip()
    if not text:
        flash('Пустой ответ')
        return redirect(url_for('post_view', post_id=c.post_id))
    if len(text) > 1000:
        flash('Слишком длинный ответ')
        return redirect(url_for('post_view', post_id=c.post_id))
    r = CommentReply(comment_id=c.id, user_id=current_user.id, text=text)
    db.session.add(r)
    db.session.commit()
    if c.user_id != current_user.id:
        snippet = text[:80] + ('…' if len(text) > 80 else '')
        notify(
            c.user_id, current_user.id, 'comment_post',
            f'💬 {current_user.username} ответил на ваш комментарий: {snippet}',
            url_for('post_view', post_id=c.post_id)
        )
    return redirect(url_for('post_view', post_id=c.post_id))


@app.route('/comment_reply/<int:reply_id>/delete', methods=['POST'])
@login_required
def comment_reply_delete(reply_id):
    r = db.session.get(CommentReply, reply_id)
    if not r:
        abort(404)
    if not current_user.is_admin and r.user_id != current_user.id:
        flash('Можно удалять только свои ответы')
        return redirect(url_for('post_view', post_id=r.comment.post_id))
    pid = r.comment.post_id
    db.session.delete(r)
    db.session.commit()
    flash('Ответ удалён')
    return redirect(url_for('post_view', post_id=pid))


@app.route('/user/<int:user_id>/follow', methods=['POST'])
@login_required
def follow_user(user_id):
    if user_id == current_user.id:
        flash('Нельзя подписаться на себя')
        return redirect(url_for('user_profile', user_id=user_id))
    target = db.session.get(User, user_id)
    if not target:
        abort(404)
    if current_user.is_following(user_id):
        flash(f'Вы уже подписаны на «{target.username}»')
        return redirect(url_for('user_profile', user_id=user_id))
    db.session.add(Subscription(follower_id=current_user.id, following_id=user_id))
    db.session.commit()
    notify(
        user_id, current_user.id, 'follow',
        f'➕ {current_user.username} подписался на вас',
        url_for('followers_page', user_id=user_id)
    )
    for sid, u in online_users.items():
        if u['id'] == user_id:
            socketio.emit('new_follower', {
                'from_id': current_user.id,
                'from_username': current_user.username,
                'from_avatar': current_user.avatar_url
            }, to=sid)
    flash(f'Вы подписались на «{target.username}»')
    return redirect(url_for('user_profile', user_id=user_id))


@app.route('/user/<int:user_id>/unfollow', methods=['POST'])
@login_required
def unfollow_user(user_id):
    target = db.session.get(User, user_id)
    if not target:
        abort(404)
    sub = Subscription.query.filter_by(
        follower_id=current_user.id, following_id=user_id
    ).first()
    if sub:
        db.session.delete(sub)
        db.session.commit()
        flash(f'Вы отписались от «{target.username}»')
    return redirect(url_for('user_profile', user_id=user_id))


@app.route('/rooms')
@login_required
def rooms_list():
    rooms = rooms_visible_to(current_user)
    return render_template('rooms.html', rooms=rooms)


@app.route('/rooms/create', methods=['POST'])
@login_required
def create_room():
    name = request.form.get('name', '').strip()
    if not name or len(name) > 64:
        flash('Некорректное имя комнаты')
        return redirect(url_for('rooms_list'))
    if Room.query.filter_by(name=name).first():
        flash('Комната с таким названием уже есть')
        return redirect(url_for('rooms_list'))
    room = Room(name=name, created_by=current_user.id)
    db.session.add(room)
    db.session.commit()
    ensure_member(room.id, current_user.id)
    return redirect(url_for('room_view', room_id=room.id))


@app.route('/rooms/<int:room_id>/delete', methods=['POST'])
@login_required
def delete_room(room_id):
    room = db.session.get(Room, room_id)
    if not room:
        abort(404)
    if room.name == 'Общий':
        flash('Общий чат удалить нельзя')
        return redirect(url_for('rooms_list'))
    if not current_user.is_admin and room.created_by != current_user.id:
        flash('Вы не можете удалить эту комнату')
        return redirect(url_for('rooms_list'))
    affected_sids = [sid for sid, u in online_users.items() if u.get('room') == room_id]
    for m in Message.query.filter_by(room_id=room_id).all():
        if m.image:
            try: os.remove(os.path.join(UPLOAD_DIR, m.image))
            except OSError: pass
        if m.file:
            try: os.remove(os.path.join(FILES_DIR, m.file))
            except OSError: pass
        MessageReaction.query.filter_by(message_id=m.id).delete()
        db.session.delete(m)
    RoomMember.query.filter_by(room_id=room_id).delete()
    RoomBan.query.filter_by(room_id=room_id).delete()
    db.session.delete(room)
    db.session.commit()
    general = Room.query.filter_by(name='Общий').first()
    general_id = general.id if general else None
    for sid in affected_sids:
        if sid in online_users:
            online_users[sid]['room'] = None
        socketio.emit('room_deleted',
                      {'room_id': room_id, 'redirect_to': general_id},
                      to=sid)
    flash(f'Комната «{room.name}» удалена')
    return redirect(url_for('rooms_list'))


@app.route('/room/<int:room_id>/clear', methods=['POST'])
@login_required
def room_clear(room_id):
    room = db.session.get(Room, room_id)
    if not room:
        abort(404)

    if room.name == 'Общий' and not current_user.is_admin:
        flash('Общий чат может очистить только администратор')
        return redirect(url_for('room_view', room_id=room_id))

    if not current_user.is_admin and room.created_by != current_user.id:
        flash('Вы не можете очистить эту комнату')
        return redirect(url_for('room_view', room_id=room_id))

    msgs = Message.query.filter_by(room_id=room_id).all()
    count = len(msgs)
    for m in msgs:
        if m.image:
            try: os.remove(os.path.join(UPLOAD_DIR, m.image))
            except OSError: pass
        if m.file:
            try: os.remove(os.path.join(FILES_DIR, m.file))
            except OSError: pass
        MessageReaction.query.filter_by(message_id=m.id).delete()
        db.session.delete(m)
    db.session.commit()

    socketio.emit('room_cleared', {
        'room_id': room_id,
        'by': current_user.username,
        'count': count,
    }, to=f'room_{room_id}')

    flash(f'Чат очищен. Удалено сообщений: {count}')
    return redirect(url_for('room_view', room_id=room_id))


@app.route('/room/<int:room_id>')
@login_required
def room_view(room_id):
    room = db.session.get(Room, room_id)
    if not room:
        abort(404)
    if is_room_banned(room_id, current_user.id):
        flash('Вы забанены в этой комнате')
        return redirect(url_for('index'))
    if not current_user.is_admin:
        is_public = (room.name == 'Общий')
        is_member = is_room_member(room_id, current_user.id)
        if not (is_public or is_member):
            flash('Эта комната приватная. Попросите добавить вас.')
            return redirect(url_for('index'))
    rooms = rooms_visible_to(current_user)
    unread = DirectMessage.unread_count(current_user.id)
    members = room_members(room_id)
    is_member = is_room_member(room_id, current_user.id)
    return render_template(
        'index.html',
        username=current_user.username,
        room=room,
        rooms=rooms,
        unread_dms=unread,
        members=members,
        is_member=is_member,
        is_banned=False
    )


@app.route('/room/<int:room_id>/join', methods=['POST'])
@login_required
def room_join(room_id):
    room = db.session.get(Room, room_id)
    if not room:
        abort(404)
    if is_room_banned(room_id, current_user.id):
        flash('Вы забанены в этой комнате')
        return redirect(url_for('index'))
    ensure_member(room_id, current_user.id)
    flash(f'Вы вступили в комнату «{room.name}»')
    return redirect(url_for('room_view', room_id=room_id))


@app.route('/room/<int:room_id>/leave', methods=['POST'])
@login_required
def room_leave(room_id):
    room = db.session.get(Room, room_id)
    if not room:
        abort(404)
    if room.name == 'Общий':
        flash('Из общего чата нельзя выйти')
        return redirect(url_for('room_view', room_id=room_id))
    RoomMember.query.filter_by(room_id=room_id, user_id=current_user.id).delete()
    db.session.commit()
    flash(f'Вы покинули комнату «{room.name}»')
    return redirect(url_for('rooms_list'))


@app.route('/room/<int:room_id>/add', methods=['POST'])
@login_required
def room_add_member(room_id):
    room = db.session.get(Room, room_id)
    if not room:
        abort(404)
    if not current_user.is_admin and not is_room_member(room_id, current_user.id):
        flash('Вы не участник этой комнаты')
        return redirect(url_for('index'))
    username = request.form.get('username', '').strip()
    if not username:
        flash('Введите имя пользователя')
        return redirect(url_for('room_view', room_id=room_id))
    user = User.query.filter_by(username=username).first()
    if not user:
        flash(f'Пользователь «{username}» не найден')
        return redirect(url_for('room_view', room_id=room_id))
    if is_room_banned(room_id, user.id):
        flash(f'«{username}» забанен в этой комнате')
        return redirect(url_for('room_view', room_id=room_id))
    if is_room_member(room_id, user.id):
        flash(f'«{username}» уже в комнате')
        return redirect(url_for('room_view', room_id=room_id))
    ensure_member(room_id, user.id)
    socketio.emit('members_update', {
        'room_id': room_id,
        'members': room_members(room_id)
    }, to=f'room_{room_id}')
    for sid, u in online_users.items():
        if u['id'] == user.id:
            socketio.emit('you_were_added', {
                'room_id': room_id,
                'room_name': room.name
            }, to=sid)
    flash(f'«{username}» добавлен в комнату')
    return redirect(url_for('room_view', room_id=room_id))


@app.route('/room/<int:room_id>/remove/<int:user_id>', methods=['POST'])
@login_required
def room_remove_member(room_id, user_id):
    room = db.session.get(Room, room_id)
    if not room:
        abort(404)
    if not current_user.is_admin and not is_room_member(room_id, current_user.id):
        flash('Вы не участник этой комнаты')
        return redirect(url_for('index'))

    if room.name == 'Общий' and not current_user.is_admin:
        flash('В общем чате удалять участников может только администратор')
        return redirect(url_for('room_view', room_id=room_id))

    target = db.session.get(User, user_id)
    if not target:
        abort(404)
    if room.name == 'Общий':
        flash('Из общего чата никого не удалить')
        return redirect(url_for('room_view', room_id=room_id))
    if target.is_admin and not current_user.is_admin:
        flash('Нельзя удалить администратора')
        return redirect(url_for('room_view', room_id=room_id))
    RoomMember.query.filter_by(room_id=room_id, user_id=user_id).delete()
    db.session.commit()
    socketio.emit('members_update', {
        'room_id': room_id,
        'members': room_members(room_id)
    }, to=f'room_{room_id}')
    for sid, u in online_users.items():
        if u['id'] == user_id and u.get('room') == room_id:
            socketio.emit('you_were_removed', {
                'room_id': room_id,
                'room_name': room.name
            }, to=sid)
            online_users[sid]['room'] = None
    flash(f'«{target.username}» удалён из комнаты')
    return redirect(url_for('room_view', room_id=room_id))


@app.route('/room/<int:room_id>/ban/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def room_ban_member(room_id, user_id):
    room = db.session.get(Room, room_id)
    if not room:
        abort(404)
    target = db.session.get(User, user_id)
    if not target:
        abort(404)
    if target.id == current_user.id:
        flash('Нельзя забанить себя')
        return redirect(url_for('room_view', room_id=room_id))
    if target.is_admin:
        flash('Нельзя забанить другого администратора')
        return redirect(url_for('room_view', room_id=room_id))
    if is_room_banned(room_id, user_id):
        flash('Уже забанен')
        return redirect(url_for('room_view', room_id=room_id))
    RoomMember.query.filter_by(room_id=room_id, user_id=user_id).delete()
    db.session.add(RoomBan(room_id=room_id, user_id=user_id, banned_by=current_user.id))
    db.session.commit()
    socketio.emit('members_update', {
        'room_id': room_id,
        'members': room_members(room_id)
    }, to=f'room_{room_id}')
    for sid, u in online_users.items():
        if u['id'] == user_id and u.get('room') == room_id:
            socketio.emit('you_were_banned', {
                'room_id': room_id,
                'room_name': room.name
            }, to=sid)
            online_users[sid]['room'] = None
    flash(f'«{target.username}» забанен в комнате')
    return redirect(url_for('room_view', room_id=room_id))


@app.route('/room/<int:room_id>/unban/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def room_unban_member(room_id, user_id):
    RoomBan.query.filter_by(room_id=room_id, user_id=user_id).delete()
    db.session.commit()
    flash('Разбанен')
    return redirect(url_for('room_view', room_id=room_id))


@app.route('/room/<int:room_id>/bans')
@login_required
@admin_required
def room_bans(room_id):
    room = db.session.get(Room, room_id)
    if not room:
        abort(404)
    bans = RoomBan.query.filter_by(room_id=room_id).all()
    return render_template('room_bans.html', room=room, bans=bans)


@app.route('/user/<int:user_id>/ban', methods=['POST'])
@login_required
@admin_required
def ban_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.id == current_user.id:
        flash('Нельзя забанить себя')
        return redirect(url_for('user_profile', user_id=user_id))
    if user.is_admin:
        flash('Нельзя забанить админа')
        return redirect(url_for('user_profile', user_id=user_id))
    user.is_banned = True
    db.session.commit()
    for sid, u in list(online_users.items()):
        if u['id'] == user_id:
            socketio.emit('you_are_banned', {}, to=sid)
            del online_users[sid]
    socketio.emit('user_banned', {'id': user_id})
    flash(f'«{user.username}» забанен глобально')
    return redirect(url_for('user_profile', user_id=user_id))


@app.route('/user/<int:user_id>/unban', methods=['POST'])
@login_required
@admin_required
def unban_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    user.is_banned = False
    db.session.commit()
    flash(f'«{user.username}» разбанен')
    return redirect(url_for('user_profile', user_id=user_id))


@app.route('/friends')
@login_required
def friends_list():
    friends = current_user.friends()
    incoming = current_user.incoming_requests()
    outgoing = current_user.outgoing_requests()
    return render_template(
        'friends.html',
        friends=friends,
        incoming=incoming,
        outgoing=outgoing
    )


@app.route('/friends/request/<int:user_id>', methods=['POST'])
@login_required
def friend_request_send(user_id):
    if user_id == current_user.id:
        flash('Нельзя добавить себя')
        return redirect(url_for('user_profile', user_id=user_id))
    target = db.session.get(User, user_id)
    if not target:
        abort(404)
    if current_user.is_friend_with(user_id):
        flash(f'«{target.username}» уже у вас в друзьях')
        return redirect(url_for('user_profile', user_id=user_id))
    if current_user.has_pending_to(user_id):
        flash(f'Заявка «{target.username}» уже отправлена')
        return redirect(url_for('user_profile', user_id=user_id))
    if current_user.has_pending_from(user_id):
        req = FriendRequest.query.filter_by(
            from_user_id=user_id, to_user_id=current_user.id, status='pending'
        ).first()
        if req:
            req.status = 'accepted'
            req.responded_at = now_msk()
            a, b = sorted([current_user.id, user_id])
            if not Friend.query.filter_by(user_id=a, friend_id=b).first():
                db.session.add(Friend(user_id=a, friend_id=b))
            db.session.commit()
            notify(user_id, current_user.id, 'friend_accept',
                   f'✅ {current_user.username} принял вашу заявку в друзья',
                   url_for('user_profile', user_id=current_user.id))
            socketio.emit('friends_update', {
                'user_id': current_user.id,
                'other_id': user_id,
                'action': 'accept'
            })
            flash(f'Вы теперь друзья с «{target.username}»')
            return redirect(url_for('user_profile', user_id=user_id))
    req = FriendRequest(
        from_user_id=current_user.id,
        to_user_id=user_id,
        status='pending'
    )
    db.session.add(req)
    db.session.commit()
    notify(user_id, current_user.id, 'friend_request',
           f'👥 {current_user.username} хочет добавить вас в друзья',
           url_for('friends_list'))
    for sid, u in online_users.items():
        if u['id'] == user_id:
            socketio.emit('friend_request_received', {
                'from_id': current_user.id,
                'from_username': current_user.username,
                'from_avatar': current_user.avatar_url
            }, to=sid)
    flash(f'Заявка в друзья отправлена «{target.username}»')
    return redirect(url_for('user_profile', user_id=user_id))


@app.route('/friends/request/<int:req_id>/accept', methods=['POST'])
@login_required
def friend_request_accept(req_id):
    req = db.session.get(FriendRequest, req_id)
    if not req or req.to_user_id != current_user.id or req.status != 'pending':
        flash('Заявка не найдена')
        return redirect(url_for('friends_list'))
    from_user = db.session.get(User, req.from_user_id)
    if not from_user:
        abort(404)
    a, b = sorted([req.from_user_id, req.to_user_id])
    if not Friend.query.filter_by(user_id=a, friend_id=b).first():
        db.session.add(Friend(user_id=a, friend_id=b))
    req.status = 'accepted'
    req.responded_at = now_msk()
    db.session.commit()
    notify(req.from_user_id, current_user.id, 'friend_accept',
           f'✅ {current_user.username} принял вашу заявку в друзья',
           url_for('user_profile', user_id=current_user.id))
    for sid, u in online_users.items():
        if u['id'] == req.from_user_id:
            socketio.emit('friend_request_accepted', {
                'by_id': current_user.id,
                'by_username': current_user.username
            }, to=sid)
    flash(f'Вы теперь друзья с «{from_user.username}»')
    return redirect(url_for('friends_list'))


@app.route('/friends/request/<int:req_id>/reject', methods=['POST'])
@login_required
def friend_request_reject(req_id):
    req = db.session.get(FriendRequest, req_id)
    if not req or req.to_user_id != current_user.id or req.status != 'pending':
        flash('Заявка не найдена')
        return redirect(url_for('friends_list'))
    req.status = 'rejected'
    req.responded_at = now_msk()
    db.session.commit()
    flash('Заявка отклонена')
    return redirect(url_for('friends_list'))


@app.route('/friends/request/<int:req_id>/cancel', methods=['POST'])
@login_required
def friend_request_cancel(req_id):
    req = db.session.get(FriendRequest, req_id)
    if not req or req.from_user_id != current_user.id or req.status != 'pending':
        flash('Заявка не найдена')
        return redirect(url_for('friends_list'))
    db.session.delete(req)
    db.session.commit()
    flash('Заявка отменена')
    return redirect(url_for('friends_list'))


@app.route('/friends/remove/<int:user_id>', methods=['POST'])
@login_required
def friend_remove(user_id):
    target = db.session.get(User, user_id)
    if not target:
        abort(404)
    a, b = sorted([current_user.id, user_id])
    row = Friend.query.filter_by(user_id=a, friend_id=b).first()
    if not row:
        flash('Не в друзьях')
        return redirect(url_for('user_profile', user_id=user_id))
    db.session.delete(row)
    FriendRequest.query.filter(
        or_(
            and_(FriendRequest.from_user_id == a, FriendRequest.to_user_id == b),
            and_(FriendRequest.from_user_id == b, FriendRequest.to_user_id == a),
        )
    ).delete()
    db.session.commit()
    socketio.emit('friends_update', {
        'user_id': current_user.id,
        'other_id': user_id,
        'action': 'remove'
    })
    flash(f'«{target.username}» удалён из друзей')
    return redirect(url_for('user_profile', user_id=user_id))


@app.route('/dms')
@login_required
def dms_list():
    me = current_user.id
    senders = db.session.query(DirectMessage.recipient_id).filter_by(sender_id=me).all()
    recipients = db.session.query(DirectMessage.sender_id).filter_by(recipient_id=me).all()
    partner_ids = {row[0] for row in senders} | {row[0] for row in recipients}
    dialogs = []
    for pid in partner_ids:
        partner = db.session.get(User, pid)
        if not partner:
            continue
        last = (DirectMessage.conversation_between(me, pid)
                .order_by(DirectMessage.created_at.desc()).first())
        unread = DirectMessage.query.filter_by(
            sender_id=pid, recipient_id=me, is_read=False).count()
        dialogs.append({'user': partner, 'last': last, 'unread': unread})
    dialogs.sort(
        key=lambda d: d['last'].created_at if d['last'] else datetime.min,
        reverse=True
    )
    return render_template('dms.html', dialogs=dialogs)


@app.route('/dm/<int:user_id>')
@login_required
def dm_view(user_id):
    partner = db.session.get(User, user_id)
    if not partner:
        abort(404)
    if partner.id == current_user.id:
        flash('Нельзя писать самому себе')
        return redirect(url_for('dms_list'))
    if not current_user.is_friend_with(partner.id):
        flash('Писать в личку можно только друзьям. Отправьте заявку.')
        return redirect(url_for('user_profile', user_id=partner.id))
    DirectMessage.query.filter_by(
        sender_id=partner.id, recipient_id=current_user.id, is_read=False
    ).update({'is_read': True})
    db.session.commit()
    socketio.emit('dm_read_by', {
        'reader_id': current_user.id,
        'partner_id': partner.id
    }, to=dm_room_name(current_user.id, partner.id))
    return render_template('dm.html', partner=partner, is_friend=True)


@app.route('/api/dm/<int:user_id>/history')
@login_required
def dm_history(user_id):
    if not current_user.is_friend_with(user_id):
        return {'error': 'not friends'}, 403
    msgs = (DirectMessage.conversation_between(current_user.id, user_id)
            .order_by(DirectMessage.created_at.asc())
            .all())
    return {'messages': [serialize_dm(m, current_user) for m in msgs]}


@app.route('/user/<int:user_id>')
@login_required
def user_profile(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    msg_count = Message.query.filter_by(user_id=user.id).count()
    posts_count = user.posts_count()
    is_self = user.id == current_user.id
    is_friend = (not is_self) and current_user.is_friend_with(user.id)
    is_following = (not is_self) and current_user.is_following(user.id)
    pending_out = (not is_self) and current_user.has_pending_to(user.id)
    pending_in = (not is_self) and current_user.has_pending_from(user.id)
    incoming_req = None
    outgoing_req = None
    if pending_in:
        incoming_req = FriendRequest.query.filter_by(
            from_user_id=user.id, to_user_id=current_user.id, status='pending'
        ).first()
    if pending_out:
        outgoing_req = FriendRequest.query.filter_by(
            from_user_id=current_user.id, to_user_id=user.id, status='pending'
        ).first()
    return render_template(
        'user.html',
        user=user,
        msg_count=msg_count,
        posts_count=posts_count,
        is_friend=is_friend,
        is_following=is_following,
        pending_out=pending_out,
        pending_in=pending_in,
        incoming_req=incoming_req,
        outgoing_req=outgoing_req
    )


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'bio':
            bio = request.form.get('bio', '').strip()
            if len(bio) > 200:
                flash('Био максимум 200 символов')
            else:
                current_user.bio = bio or None
                db.session.commit()
                flash('Био обновлено')
        elif action == 'education':
            university = request.form.get('university', '').strip()[:120]
            faculty = request.form.get('faculty', '').strip()[:120]
            course = request.form.get('course', '').strip()[:32]
            current_user.university = university or None
            current_user.faculty = faculty or None
            current_user.course = course or None
            db.session.commit()
            flash('Данные об обучении обновлены')
        elif action == 'password':
            old = request.form.get('old_password', '')
            new = request.form.get('new_password', '')
            new2 = request.form.get('new_password2', '')
            if not current_user.check_password(old):
                flash('Неверный текущий пароль')
            elif len(new) < 4:
                flash('Новый пароль минимум 4 символа')
            elif new != new2:
                flash('Новые пароли не совпадают')
            else:
                current_user.set_password(new)
                db.session.commit()
                flash('Пароль изменён')
        elif action == 'email':
            new_email = request.form.get('new_email', '').strip().lower()
            if not EMAIL_RE.match(new_email):
                flash('Некорректный email')
            elif User.query.filter(User.email == new_email, User.id != current_user.id).first():
                flash('Этот email уже занят')
            else:
                current_user.email = new_email
                db.session.commit()
                flash('Email обновлён')
        elif action == 'sound':
            s = current_user.get_settings()
            s.sound_enabled = bool(request.form.get('sound_enabled'))
            try:
                s.sound_volume = max(0, min(100, int(request.form.get('sound_volume', 70))))
            except (TypeError, ValueError):
                s.sound_volume = 70
            s.sound_dm = bool(request.form.get('sound_dm'))
            s.sound_group = bool(request.form.get('sound_group'))
            s.sound_notify = bool(request.form.get('sound_notify'))
            db.session.commit()
            flash('Настройки звука сохранены')
        return redirect(url_for('profile'))
    msg_count = Message.query.filter_by(user_id=current_user.id).count()
    return render_template('profile.html', msg_count=msg_count)


@app.route('/profile/avatar', methods=['POST'])
@login_required
def upload_avatar():
    if 'avatar' not in request.files:
        flash('Файл не выбран')
        return redirect(url_for('profile'))
    f = request.files['avatar']
    if not f or not f.filename:
        flash('Файл не выбран')
        return redirect(url_for('profile'))
    if not is_image(f.filename):
        flash('Аватар должен быть картинкой')
        return redirect(url_for('profile'))
    try:
        fname = save_avatar(f)
    except Exception as e:
        flash(f'Ошибка обработки: {e}')
        return redirect(url_for('profile'))
    if current_user.avatar:
        try: os.remove(os.path.join(AVATAR_DIR, current_user.avatar))
        except OSError: pass
    current_user.avatar = fname
    db.session.commit()
    socketio.emit('user_updated', {
        'id': current_user.id,
        'username': current_user.username,
        'avatar': current_user.avatar_url
    })
    flash('Аватар обновлён')
    return redirect(url_for('profile'))


@app.route('/profile/avatar/delete', methods=['POST'])
@login_required
def delete_avatar():
    if current_user.avatar:
        try: os.remove(os.path.join(AVATAR_DIR, current_user.avatar))
        except OSError: pass
        current_user.avatar = None
        db.session.commit()
        socketio.emit('user_updated', {
            'id': current_user.id,
            'username': current_user.username,
            'avatar': None
        })
        flash('Аватар удалён')
    return redirect(url_for('profile'))


@app.route('/account/delete', methods=['POST'])
@login_required
def delete_account():
    password = request.form.get('password', '')
    if not current_user.check_password(password):
        flash('Неверный пароль')
        return redirect(url_for('profile'))
    user = current_user._get_current_object()
    uid = user.id
    uname = user.username
    if user.avatar:
        try: os.remove(os.path.join(AVATAR_DIR, user.avatar))
        except OSError: pass
    for p in Post.query.filter_by(user_id=uid).all():
        if p.image:
            try: os.remove(os.path.join(UPLOAD_DIR, p.image))
            except OSError: pass
        if p.file:
            try: os.remove(os.path.join(FILES_DIR, p.file))
            except OSError: pass
        db.session.delete(p)
    for c in PostComment.query.filter_by(user_id=uid).all():
        CommentReply.query.filter_by(comment_id=c.id).delete()
        CommentLike.query.filter_by(comment_id=c.id).delete()
        db.session.delete(c)
    CommentReply.query.filter_by(user_id=uid).delete()
    CommentLike.query.filter_by(user_id=uid).delete()
    PostLike.query.filter_by(user_id=uid).delete()
    PostView.query.filter_by(user_id=uid).delete()
    for m in Message.query.filter_by(user_id=uid).all():
        if m.image:
            try: os.remove(os.path.join(UPLOAD_DIR, m.image))
            except OSError: pass
        if m.file:
            try: os.remove(os.path.join(FILES_DIR, m.file))
            except OSError: pass
        db.session.delete(m)
    MessageReaction.query.filter_by(user_id=uid).delete()
    dms = DirectMessage.query.filter(
        or_(DirectMessage.sender_id == uid, DirectMessage.recipient_id == uid)
    ).all()
    for dm in dms:
        if dm.image:
            try: os.remove(os.path.join(UPLOAD_DIR, dm.image))
            except OSError: pass
        if dm.file:
            try: os.remove(os.path.join(FILES_DIR, dm.file))
            except OSError: pass
        db.session.delete(dm)
    RoomMember.query.filter_by(user_id=uid).delete()
    RoomBan.query.filter_by(user_id=uid).delete()
    Friend.query.filter(or_(Friend.user_id == uid, Friend.friend_id == uid)).delete()
    FriendRequest.query.filter(
        or_(FriendRequest.from_user_id == uid, FriendRequest.to_user_id == uid)
    ).delete()
    Subscription.query.filter(
        or_(Subscription.follower_id == uid, Subscription.following_id == uid)
    ).delete()
    Notification.query.filter(
        or_(Notification.user_id == uid, Notification.actor_id == uid)
    ).delete()
    NotificationSetting.query.filter_by(user_id=uid).delete()
    for room in Room.query.filter_by(created_by=uid).all():
        room.created_by = None
    for sid, u in list(online_users.items()):
        if u['id'] == uid:
            del online_users[sid]
    db.session.delete(user)
    db.session.commit()
    socketio.emit('user_deleted', {'id': uid, 'username': uname})
    logout_user()
    flash('Аккаунт удалён. Все данные стёрты.')
    return redirect(url_for('login'))


@app.route('/uploads/<path:filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route('/files/<path:filename>')
@login_required
def serve_file(filename):
    download_name = request.args.get('name')
    as_attachment = request.args.get('dl') == '1'
    if as_attachment:
        return send_from_directory(FILES_DIR, filename,
                                   as_attachment=True,
                                   download_name=download_name or filename)
    return send_from_directory(FILES_DIR, filename)


@app.route('/upload_image', methods=['POST'])
@login_required
def upload_image():
    if 'image' not in request.files:
        return {'error': 'no file'}, 400
    f = request.files['image']
    if not f or not f.filename:
        return {'error': 'empty'}, 400
    if not is_image(f.filename):
        return {'error': 'Не картинка'}, 400
    try:
        fname = save_image(f)
    except Exception as e:
        return {'error': f'Ошибка: {e}'}, 500
    return {'filename': fname}


@app.route('/upload_file', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return {'error': 'no file'}, 400
    f = request.files['file']
    if not f or not f.filename:
        return {'error': 'empty'}, 400
    try:
        fname, original, mime = save_file(f)
    except Exception as e:
        return {'error': f'Ошибка: {e}'}, 500
    size = os.path.getsize(os.path.join(FILES_DIR, fname))
    return {
        'filename': fname,
        'original': original,
        'mime': mime,
        'size': size,
        'size_human': human_size(size)
    }


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')
        if not username or not email or not password:
            flash('Заполните все поля')
        elif len(username) < 3 or len(username) > 32:
            flash('Имя от 3 до 32 символов')
        elif not EMAIL_RE.match(email):
            flash('Некорректный email')
        elif len(password) < 4:
            flash('Пароль минимум 4 символа')
        elif password != password2:
            flash('Пароли не совпадают')
        elif User.query.filter_by(username=username).first():
            flash('Такое имя уже занято')
        elif User.query.filter_by(email=email).first():
            flash('Этот email уже зарегистрирован')
        else:
            user = User(username=username, email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            db.session.add(NotificationSetting(user_id=user.id))
            db.session.commit()
            login_user(user)
            return redirect(url_for('profile'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        login_value = request.form.get('login', '').strip()
        password = request.form.get('password', '')
        user = find_user_by_login(login_value)
        if user and user.check_password(password):
            if user.is_banned:
                flash('Ваш аккаунт забанен администратором')
                return render_template('login.html')
            login_user(user)
            return redirect(url_for('index'))
        flash('Неверные данные для входа')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@socketio.on('connect')
def on_connect():
    if not current_user.is_authenticated:
        return False
    if current_user.is_banned:
        return False
    online_users[request.sid] = {
        'id': current_user.id,
        'username': current_user.username,
        'room': None
    }
    for room_name in dm_room_names_for(current_user.id):
        join_room(room_name)
    emit('friend_requests_count', {'count': current_user.incoming_count()})
    emit('notifications_count', {'count': current_user.unread_notifications_count()})


@socketio.on('join_room')
def on_join_room(data):
    if not current_user.is_authenticated:
        return
    if current_user.is_banned:
        return
    room_id = data.get('room_id')
    room = db.session.get(Room, room_id)
    if not room:
        return
    if is_room_banned(room_id, current_user.id):
        emit('error_msg', {'msg': 'Вы забанены в этой комнате'})
        return
    if not current_user.is_admin:
        is_public = (room.name == 'Общий')
        is_member = is_room_member(room_id, current_user.id)
        if not (is_public or is_member):
            emit('error_msg', {'msg': 'Эта комната приватная'})
            return
    prev = online_users[request.sid].get('room')
    if prev and prev != room_id:
        leave_room(f'room_{prev}')
    join_room(f'room_{room_id}')
    online_users[request.sid]['room'] = room_id
    history = (Message.query
               .filter_by(room_id=room_id)
               .order_by(Message.created_at.asc())
               .all())
    emit('history', {'messages': [serialize_message(m, current_user) for m in history]})
    socketio.emit('users_list', users_in_room(room_id), to=f'room_{room_id}')
    emit('members_update', {'room_id': room_id, 'members': room_members(room_id)})
    emit('system_msg', {
        'msg': f'Вы вошли в комнату «{room.name}»',
        'time': now_str()
    })


@socketio.on('message')
def on_message(data):
    if not current_user.is_authenticated:
        return
    if current_user.is_banned:
        return
    room_id = online_users[request.sid].get('room')
    if not room_id:
        return
    if is_room_banned(room_id, current_user.id):
        emit('error_msg', {'msg': 'Вы забанены в этой комнате'})
        return
    room = db.session.get(Room, room_id)
    if room and not current_user.is_admin:
        is_public = (room.name == 'Общий')
        is_member = is_room_member(room_id, current_user.id)
        if not (is_public or is_member):
            emit('error_msg', {'msg': 'Эта комната приватная'})
            return
    text = (data.get('msg') or '').strip()
    image = data.get('image')
    file = data.get('file')
    file_name = data.get('file_name')
    file_type = data.get('file_type')
    reply_to_id = data.get('reply_to_id')
    if not text and not image and not file:
        return
    if len(text) > 1000:
        return
    msg = Message(
        user_id=current_user.id, room_id=room_id,
        text=text or None,
        image=image or None,
        file=file or None,
        file_name=file_name or None,
        file_type=file_type or None,
        reply_to_id=reply_to_id or None
    )
    db.session.add(msg)
    db.session.commit()
    ensure_member(room_id, current_user.id)
    socketio.emit('message', serialize_message(msg, current_user), to=f'room_{room_id}')
    try:
        if room and room.name != 'Общий':
            members = RoomMember.query.filter_by(room_id=room_id).all()
            preview = text[:60] if text else ('📷 фото' if image else ('🎤 голосовое' if file_type and 'audio' in file_type else '📎 файл'))
            for m in members:
                if m.user_id != current_user.id:
                    notify(
                        m.user_id, current_user.id, 'group_msg',
                        f'💬 #{room.name} · {current_user.username}: {preview}',
                        url_for('room_view', room_id=room_id)
                    )
    except Exception as e:
        logging.warning(f'notify group_msg: {e}')


@socketio.on('edit_message')
def on_edit_message(data):
    if not current_user.is_authenticated:
        return
    if current_user.is_banned:
        return
    msg_id = data.get('id')
    new_text = (data.get('msg') or '').strip()
    msg = db.session.get(Message, msg_id)
    if not msg:
        return
    if not current_user.is_admin and msg.user_id != current_user.id:
        emit('error_msg', {'msg': 'Можно редактировать только свои сообщения'})
        return
    if not new_text or len(new_text) > 1000:
        return
    msg.text = new_text
    msg.edited_at = now_msk()
    db.session.commit()
    socketio.emit('message_edited', {
        'id': msg.id, 'msg': msg.text, 'edited': True
    }, to=f'room_{msg.room_id}')


@socketio.on('delete_message')
def on_delete_message(data):
    if not current_user.is_authenticated:
        return
    msg_id = data.get('id')
    msg = db.session.get(Message, msg_id)
    if not msg:
        return
    if not current_user.is_admin and msg.user_id != current_user.id:
        emit('error_msg', {'msg': 'Можно удалять только свои сообщения'})
        return
    if msg.image:
        try: os.remove(os.path.join(UPLOAD_DIR, msg.image))
        except OSError: pass
    if msg.file:
        try: os.remove(os.path.join(FILES_DIR, msg.file))
        except OSError: pass
    room_id = msg.room_id
    db.session.delete(msg)
    db.session.commit()
    socketio.emit('message_deleted', {'id': msg_id}, to=f'room_{room_id}')


@socketio.on('typing_room')
def on_typing_room(data):
    if not current_user.is_authenticated:
        return
    if current_user.is_banned:
        return
    room_id = online_users[request.sid].get('room')
    if not room_id:
        return
    is_typing = bool(data.get('typing'))
    socketio.emit('typing_room_update', {
        'room_id': room_id,
        'user_id': current_user.id,
        'username': current_user.username,
        'typing': is_typing,
    }, to=f'room_{room_id}', include_self=False)


@socketio.on('typing_dm')
def on_typing_dm(data):
    if not current_user.is_authenticated:
        return
    if current_user.is_banned:
        return
    partner_id = data.get('partner_id')
    if not partner_id:
        return
    partner = db.session.get(User, partner_id)
    if not partner or not current_user.is_friend_with(partner_id):
        return
    is_typing = bool(data.get('typing'))
    socketio.emit('typing_dm_update', {
        'user_id': current_user.id,
        'username': current_user.username,
        'typing': is_typing,
    }, to=dm_room_name(current_user.id, partner_id), include_self=False)


@socketio.on('react_message')
def on_react_message(data):
    if not current_user.is_authenticated:
        return
    if current_user.is_banned:
        return
    msg_id = data.get('message_id')
    emoji = (data.get('emoji') or '').strip()
    scope = data.get('scope') or 'room'
    if not msg_id or emoji not in REACTION_EMOJIS:
        return
    msg = db.session.get(Message, msg_id)
    if not msg:
        return
    existing = MessageReaction.query.filter_by(
        message_id=msg_id, user_id=current_user.id, emoji=emoji
    ).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
    else:
        db.session.add(MessageReaction(
            message_id=msg_id,
            user_id=current_user.id,
            emoji=emoji,
        ))
        db.session.commit()
        try:
            if msg.user_id != current_user.id:
                preview = (msg.text or '')[:60] if msg.text else ('📷 фото' if msg.image else ('🎤 голосовое' if msg.file_type and 'audio' in msg.file_type else '📎 файл'))
                room = db.session.get(Room, msg.room_id)
                room_name = room.name if room else 'комната'
                notify(
                    msg.user_id,
                    current_user.id,
                    'reaction',
                    f'{emoji} {current_user.username} отреагировал на ваше сообщение в #{room_name}: {preview}',
                    url_for('room_view', room_id=msg.room_id)
                )
        except Exception as e:
            logging.warning(f'notify reaction: {e}')
    rx = reactions_for_message(msg_id, current_user.id)
    payload = {
        'id': msg_id,
        'reactions': rx,
        'scope': scope,
    }
    if scope == 'dm':
        sender_id = data.get('sender_id')
        recipient_id = data.get('recipient_id')
        if sender_id and recipient_id:
            socketio.emit('message_reactions', payload,
                          to=dm_room_name(sender_id, recipient_id))
    else:
        socketio.emit('message_reactions', payload, to=f'room_{msg.room_id}')


@socketio.on('dm_message')
def on_dm_message(data):
    if not current_user.is_authenticated or current_user.is_banned:
        return
    recipient_id = data.get('recipient_id')
    text = (data.get('msg') or '').strip()
    image = data.get('image')
    file = data.get('file')
    file_name = data.get('file_name')
    file_type = data.get('file_type')
    reply_to_id = data.get('reply_to_id')
    partner = db.session.get(User, recipient_id)
    if not partner or partner.id == current_user.id:
        return
    if not current_user.is_friend_with(partner.id):
        emit('error_msg', {'msg': 'Писать в личку можно только друзьям'})
        return
    if not text and not image and not file:
        return
    if len(text) > 1000:
        return
    dm = DirectMessage(
        sender_id=current_user.id, recipient_id=partner.id,
        text=text or None,
        image=image or None,
        file=file or None,
        file_name=file_name or None,
        file_type=file_type or None,
        reply_to_id=reply_to_id or None,
        is_read=False
    )
    db.session.add(dm)
    db.session.commit()
    room_name = dm_room_name(current_user.id, partner.id)
    socketio.emit('dm_message', serialize_dm(dm, current_user), to=room_name)
    socketio.emit('dm_unread_update', {
        'user_id': partner.id,
        'count': DirectMessage.unread_count(partner.id)
    })
    preview = text[:80] if text else ('📷 фото' if image else ('🎤 голосовое' if file_type and 'audio' in file_type else '📎 файл'))
    notify(
        partner.id, current_user.id, 'dm',
        f'✉️ {current_user.username}: {preview}',
        url_for('dm_view', user_id=current_user.id)
    )


@socketio.on('dm_edit')
def on_dm_edit(data):
    if not current_user.is_authenticated:
        return
    dm_id = data.get('id')
    new_text = (data.get('msg') or '').strip()
    dm = db.session.get(DirectMessage, dm_id)
    if not dm:
        return
    if not current_user.is_admin and dm.sender_id != current_user.id:
        emit('error_msg', {'msg': 'Можно редактировать только свои сообщения'})
        return
    if not new_text or len(new_text) > 1000:
        return
    dm.text = new_text
    dm.edited_at = now_msk()
    db.session.commit()
    socketio.emit('dm_message_edited', {
        'id': dm.id, 'msg': dm.text, 'edited': True
    }, to=dm_room_name(dm.sender_id, dm.recipient_id))


@socketio.on('dm_read')
def on_dm_read(data):
    if not current_user.is_authenticated:
        return
    partner_id = data.get('partner_id')
    DirectMessage.query.filter_by(
        sender_id=partner_id, recipient_id=current_user.id, is_read=False
    ).update({'is_read': True})
    db.session.commit()
    socketio.emit('dm_read_by', {
        'reader_id': current_user.id,
        'partner_id': partner_id
    }, to=dm_room_name(current_user.id, partner_id))
    emit('dm_unread_update', {
        'user_id': current_user.id,
        'count': DirectMessage.unread_count(current_user.id)
    })


@socketio.on('dm_delete')
def on_dm_delete(data):
    if not current_user.is_authenticated:
        return
    dm_id = data.get('id')
    dm = db.session.get(DirectMessage, dm_id)
    if not dm:
        return
    if not current_user.is_admin and dm.sender_id != current_user.id:
        emit('error_msg', {'msg': 'Можно удалять только свои сообщения'})
        return
    if dm.image:
        try: os.remove(os.path.join(UPLOAD_DIR, dm.image))
        except OSError: pass
    if dm.file:
        try: os.remove(os.path.join(FILES_DIR, dm.file))
        except OSError: pass
    sender_id, recipient_id = dm.sender_id, dm.recipient_id
    db.session.delete(dm)
    db.session.commit()
    socketio.emit('dm_message_deleted', {'id': dm_id},
                  to=dm_room_name(sender_id, recipient_id))


@socketio.on('disconnect')
def on_disconnect():
    user = online_users.pop(request.sid, None)
    if user and user.get('room'):
        socketio.emit('users_list',
                      users_in_room(user['room']),
                      to=f'room_{user["room"]}')


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        if not Room.query.filter_by(name='Общий').first():
            db.session.add(Room(name='Общий'))
            db.session.commit()
    try:
        print("⏳ Предзагрузка новостей...")
        fetch_news(force=True)
        print(f"✅ Загружено {len(_news_cache['data'])} новостей")
    except Exception as e:
        print(f"⚠️ Не удалось загрузить новости: {e}")
    print("=" * 55)
    print("   ГАЗ.МЕССЕНДЖЕР")
    print("   Created by Sergej Fedorkin")
    print(f"   Telegram: {TELEGRAM_URL}")
    print("   http://localhost:5000")
    print("=" * 55)
    socketio.run(app, host='0.0.0.0', port=5000)
    
#ngrok http 5000