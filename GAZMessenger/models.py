from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, timezone, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import or_, and_

db = SQLAlchemy()

MSK = timezone(timedelta(hours=3))


def now_msk():
    return datetime.now(MSK).replace(tzinfo=None)


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    avatar = db.Column(db.String(255), nullable=True)
    bio = db.Column(db.String(200), nullable=True)
    university = db.Column(db.String(120), nullable=True)
    faculty = db.Column(db.String(120), nullable=True)
    course = db.Column(db.String(32), nullable=True)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_banned = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=now_msk)

    messages = db.relationship(
        'Message',
        backref='author',
        lazy=True,
        foreign_keys='Message.user_id'
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def avatar_url(self):
        if self.avatar:
            return f'/uploads/avatars/{self.avatar}'
        return None

    def friends(self):
        rows = Friend.query.filter(
            or_(Friend.user_id == self.id, Friend.friend_id == self.id)
        ).all()
        ids = {r.friend_id if r.user_id == self.id else r.user_id for r in rows}
        if not ids:
            return []
        return User.query.filter(User.id.in_(ids)).order_by(User.username).all()

    def friend_ids(self):
        rows = Friend.query.filter(
            or_(Friend.user_id == self.id, Friend.friend_id == self.id)
        ).all()
        return {r.friend_id if r.user_id == self.id else r.user_id for r in rows}

    def is_friend_with(self, user_id):
        if user_id == self.id:
            return False
        a, b = sorted([self.id, user_id])
        return Friend.query.filter_by(user_id=a, friend_id=b).first() is not None

    def has_pending_to(self, user_id):
        return FriendRequest.query.filter_by(
            from_user_id=self.id, to_user_id=user_id, status='pending'
        ).first() is not None

    def has_pending_from(self, user_id):
        return FriendRequest.query.filter_by(
            from_user_id=user_id, to_user_id=self.id, status='pending'
        ).first() is not None

    def incoming_requests(self):
        return (FriendRequest.query
                .filter_by(to_user_id=self.id, status='pending')
                .order_by(FriendRequest.created_at.desc()).all())

    def outgoing_requests(self):
        return (FriendRequest.query
                .filter_by(from_user_id=self.id, status='pending')
                .order_by(FriendRequest.created_at.desc()).all())

    def incoming_count(self):
        return FriendRequest.query.filter_by(
            to_user_id=self.id, status='pending').count()

    def followers(self):
        subs = Subscription.query.filter_by(following_id=self.id).all()
        ids = [s.follower_id for s in subs]
        if not ids:
            return []
        return User.query.filter(User.id.in_(ids)).order_by(User.username).all()

    def following(self):
        subs = Subscription.query.filter_by(follower_id=self.id).all()
        ids = [s.following_id for s in subs]
        if not ids:
            return []
        return User.query.filter(User.id.in_(ids)).order_by(User.username).all()

    def is_following(self, user_id):
        return Subscription.query.filter_by(
            follower_id=self.id, following_id=user_id).first() is not None

    def followers_count(self):
        return Subscription.query.filter_by(following_id=self.id).count()

    def following_count(self):
        return Subscription.query.filter_by(follower_id=self.id).count()

    def posts_count(self):
        return Post.query.filter_by(user_id=self.id).count()

    def notifications(self):
        return (Notification.query
                .filter_by(user_id=self.id)
                .order_by(Notification.created_at.desc()).all())

    def unread_notifications_count(self):
        return Notification.query.filter_by(
            user_id=self.id, is_read=False).count()

    def get_settings(self):
        s = NotificationSetting.query.filter_by(user_id=self.id).first()
        if not s:
            s = NotificationSetting(user_id=self.id)
            db.session.add(s)
            db.session.commit()
        return s


class NotificationSetting(db.Model):
    __tablename__ = 'notification_settings'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)

    notify_dm = db.Column(db.Boolean, default=True, nullable=False)
    notify_group = db.Column(db.Boolean, default=True, nullable=False)
    notify_like = db.Column(db.Boolean, default=True, nullable=False)
    notify_comment = db.Column(db.Boolean, default=True, nullable=False)
    notify_follow = db.Column(db.Boolean, default=True, nullable=False)
    notify_friend = db.Column(db.Boolean, default=True, nullable=False)
    notify_new_post = db.Column(db.Boolean, default=True, nullable=False)
    notify_world_news = db.Column(db.Boolean, default=False, nullable=False)

    sound_enabled = db.Column(db.Boolean, default=True, nullable=False)
    sound_volume = db.Column(db.Integer, default=70, nullable=False)
    sound_dm = db.Column(db.Boolean, default=True, nullable=False)
    sound_group = db.Column(db.Boolean, default=True, nullable=False)
    sound_notify = db.Column(db.Boolean, default=True, nullable=False)


class Notification(db.Model):
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    type = db.Column(db.String(32), nullable=False, index=True)
    link = db.Column(db.String(500), nullable=True)
    text = db.Column(db.String(500), nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=now_msk, index=True)

    user = db.relationship('User', foreign_keys=[user_id])
    actor = db.relationship('User', foreign_keys=[actor_id])


class Subscription(db.Model):
    __tablename__ = 'subscriptions'

    id = db.Column(db.Integer, primary_key=True)
    follower_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    following_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=now_msk)

    __table_args__ = (
        db.UniqueConstraint('follower_id', 'following_id', name='uq_sub_pair'),
    )


class FriendRequest(db.Model):
    __tablename__ = 'friend_requests'

    id = db.Column(db.Integer, primary_key=True)
    from_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    to_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    status = db.Column(db.String(16), default='pending', nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=now_msk)
    responded_at = db.Column(db.DateTime, nullable=True)

    from_user = db.relationship('User', foreign_keys=[from_user_id])
    to_user = db.relationship('User', foreign_keys=[to_user_id])

    __table_args__ = (
        db.UniqueConstraint('from_user_id', 'to_user_id', name='uq_friend_req_pair'),
    )


class Friend(db.Model):
    __tablename__ = 'friends'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    friend_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=now_msk)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'friend_id', name='uq_friend_pair'),
    )


class Post(db.Model):
    __tablename__ = 'posts'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    text = db.Column(db.Text, nullable=True)
    image = db.Column(db.String(255), nullable=True)
    file = db.Column(db.String(255), nullable=True)
    file_name = db.Column(db.String(255), nullable=True)
    file_type = db.Column(db.String(64), nullable=True)
    views = db.Column(db.Integer, default=0, nullable=False)
    edited_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=now_msk, index=True)

    author = db.relationship('User', foreign_keys=[user_id])
    likes = db.relationship('PostLike', backref='post', lazy=True,
                            cascade='all, delete-orphan')
    comments = db.relationship('PostComment', backref='post', lazy=True,
                               cascade='all, delete-orphan')
    views_rel = db.relationship('PostView', backref='post', lazy=True,
                                cascade='all, delete-orphan')

    def likes_count(self):
        return PostLike.query.filter_by(post_id=self.id).count()

    def comments_count(self):
        return PostComment.query.filter_by(post_id=self.id).count()

    def is_liked_by(self, user_id):
        return PostLike.query.filter_by(post_id=self.id, user_id=user_id).first() is not None


class PostLike(db.Model):
    __tablename__ = 'post_likes'

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('posts.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=now_msk)

    user = db.relationship('User', foreign_keys=[user_id])

    __table_args__ = (
        db.UniqueConstraint('post_id', 'user_id', name='uq_post_like'),
    )


class PostComment(db.Model):
    __tablename__ = 'post_comments'

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('posts.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=now_msk, index=True)

    author = db.relationship('User', foreign_keys=[user_id])

    def likes_count(self):
        return CommentLike.query.filter_by(comment_id=self.id).count()

    def is_liked_by(self, user_id):
        return CommentLike.query.filter_by(comment_id=self.id, user_id=user_id).first() is not None

    def replies(self):
        return (CommentReply.query
                .filter_by(comment_id=self.id)
                .order_by(CommentReply.created_at)
                .all())


class CommentLike(db.Model):
    __tablename__ = 'comment_likes'

    id = db.Column(db.Integer, primary_key=True)
    comment_id = db.Column(db.Integer, db.ForeignKey('post_comments.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=now_msk)

    user = db.relationship('User', foreign_keys=[user_id])

    __table_args__ = (
        db.UniqueConstraint('comment_id', 'user_id', name='uq_comment_like'),
    )


class CommentReply(db.Model):
    __tablename__ = 'comment_replies'

    id = db.Column(db.Integer, primary_key=True)
    comment_id = db.Column(db.Integer, db.ForeignKey('post_comments.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=now_msk, index=True)

    author = db.relationship('User', foreign_keys=[user_id])


class PostView(db.Model):
    __tablename__ = 'post_views'

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('posts.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=now_msk)

    __table_args__ = (
        db.UniqueConstraint('post_id', 'user_id', name='uq_post_view'),
    )


class Room(db.Model):
    __tablename__ = 'rooms'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=now_msk)

    messages = db.relationship('Message', backref='room', lazy=True)


class RoomMember(db.Model):
    __tablename__ = 'room_members'

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    joined_at = db.Column(db.DateTime, default=now_msk)

    __table_args__ = (
        db.UniqueConstraint('room_id', 'user_id', name='uq_room_user'),
    )

    user = db.relationship('User', foreign_keys=[user_id])
    room = db.relationship('Room', foreign_keys=[room_id])


class RoomBan(db.Model):
    __tablename__ = 'room_bans'

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    banned_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    banned_at = db.Column(db.DateTime, default=now_msk)

    __table_args__ = (
        db.UniqueConstraint('room_id', 'user_id', name='uq_ban_room_user'),
    )

    user = db.relationship('User', foreign_keys=[user_id])
    room = db.relationship('Room', foreign_keys=[room_id])


class Message(db.Model):
    __tablename__ = 'messages'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False, index=True)
    text = db.Column(db.Text, nullable=True)
    image = db.Column(db.String(255), nullable=True)
    file = db.Column(db.String(255), nullable=True)
    file_name = db.Column(db.String(255), nullable=True)
    file_type = db.Column(db.String(64), nullable=True)
    reply_to_id = db.Column(db.Integer, nullable=True)
    edited_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=now_msk, index=True)

    reactions = db.relationship('MessageReaction', backref='message', lazy=True,
                                cascade='all, delete-orphan')


class MessageReaction(db.Model):
    __tablename__ = 'message_reactions'

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('messages.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    emoji = db.Column(db.String(16), nullable=False)
    created_at = db.Column(db.DateTime, default=now_msk)

    user = db.relationship('User', foreign_keys=[user_id])

    __table_args__ = (
        db.UniqueConstraint('message_id', 'user_id', 'emoji', name='uq_message_reaction'),
    )


class DirectMessage(db.Model):
    __tablename__ = 'direct_messages'

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    text = db.Column(db.Text, nullable=True)
    image = db.Column(db.String(255), nullable=True)
    file = db.Column(db.String(255), nullable=True)
    file_name = db.Column(db.String(255), nullable=True)
    file_type = db.Column(db.String(64), nullable=True)
    reply_to_id = db.Column(db.Integer, nullable=True)
    is_read = db.Column(db.Boolean, default=False, nullable=False, index=True)
    edited_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=now_msk, index=True)

    sender = db.relationship('User', foreign_keys=[sender_id])
    recipient = db.relationship('User', foreign_keys=[recipient_id])

    @staticmethod
    def conversation_between(user_a, user_b):
        return DirectMessage.query.filter(
            or_(
                and_(DirectMessage.sender_id == user_a,
                     DirectMessage.recipient_id == user_b),
                and_(DirectMessage.sender_id == user_b,
                     DirectMessage.recipient_id == user_a)
            )
        )

    @staticmethod
    def unread_count(user_id):
        return DirectMessage.query.filter_by(
            recipient_id=user_id, is_read=False).count()