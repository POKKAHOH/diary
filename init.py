from app import app, db, Day

with app.app_context():
    db.create_all()  # создаём все таблицы
    days = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
    for i, name in enumerate(days, start=1):
        db.session.add(Day(name=name, order=i))
    db.session.commit()
    print("База данных создана и дни добавлены")
