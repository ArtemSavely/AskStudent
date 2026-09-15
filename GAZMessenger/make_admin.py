"""
Управление админкой.
Использование:
    python make_admin.py <username>          # сделать админом
    python make_admin.py <username> --remove # снять админку
    python make_admin.py <username> --toggle # переключить
"""
import sys
from app import app
from models import db, User


def show_status(user):
    status = "👑 АДМИН" if user.is_admin else "обычный пользователь"
    print(f"   {user.username} — {status}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    username = sys.argv[1]
    mode = 'grant'
    if len(sys.argv) >= 3:
        if sys.argv[2] in ('--remove', '-r', 'remove'):
            mode = 'remove'
        elif sys.argv[2] in ('--toggle', '-t', 'toggle'):
            mode = 'toggle'
        elif sys.argv[2] in ('--grant', '-g', 'grant'):
            mode = 'grant'
        else:
            print(f"❌ Неизвестный флаг: {sys.argv[2]}")
            print(__doc__)
            sys.exit(1)

    with app.app_context():
        user = User.query.filter_by(username=username).first()
        if not user:
            print(f"❌ Пользователь '{username}' не найден")
            sys.exit(1)

        print(f"\n📋 До изменения:")
        show_status(user)

        if mode == 'grant':
            if user.is_admin:
                print(f"\nℹ️  {username} уже админ. Ничего не меняю.")
                sys.exit(0)
            user.is_admin = True
            db.session.commit()
            print(f"\n✅ {username} теперь админ")

        elif mode == 'remove':
            if not user.is_admin:
                print(f"\nℹ️  {username} и так не админ. Ничего не меняю.")
                sys.exit(0)
            user.is_admin = False
            db.session.commit()
            print(f"\n🔻 С {username} снята админка")

        elif mode == 'toggle':
            user.is_admin = not user.is_admin
            db.session.commit()
            action = "выдан" if user.is_admin else "снят"
            print(f"\n🔁 Статус админа {action} для {username}")

        print(f"\n📋 После изменения:")
        show_status(user)
        print()