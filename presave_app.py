from flask import Flask, render_template, request, Response, stream_with_context, session, redirect, url_for, flash, abort
from datetime import date, timedelta, datetime
import locale
import requests
import yt_dlp
from flask_sqlalchemy import SQLAlchemy
import os
import json
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(app.root_path, 'school.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

API_KEY = os.getenv("API_KEY")
app.secret_key = os.getenv("app.secret_key")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
START_DATE = date(2025, 9, 1)  # 1 сентября 2025 – понедельник первой недели
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024   # 50 МБ

# Попытка установить русскую локаль
try:
    locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
except:
    pass

# Модели базы данных
class Day(db.Model):
    __tablename__ = 'days'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), unique=True, nullable=False)
    order = db.Column(db.Integer, unique=True, nullable=False)
    lessons = db.relationship('Lesson', backref='day', lazy=True, cascade='all, delete-orphan')

class Lesson(db.Model):
    __tablename__ = 'lessons'
    __table_args__ = (
        db.UniqueConstraint('day_id', 'lesson_number', 'week', name='unique_lesson_per_week'),
    )
    id = db.Column(db.Integer, primary_key=True)
    day_id = db.Column(db.Integer, db.ForeignKey('days.id'), nullable=False)
    week = db.Column(db.Integer, nullable=False, default=1)
    lesson_number = db.Column(db.Integer, nullable=False)
    subject = db.Column(db.String(100), nullable=False)
    topic = db.Column(db.String(200), nullable=False)
    homework = db.Column(db.Text, default='')
    videos = db.relationship('Video', backref='lesson', lazy=True, cascade='all, delete-orphan')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Video(db.Model):
    __tablename__ = 'videos'
    id = db.Column(db.Integer, primary_key=True)
    lesson_id = db.Column(db.Integer, db.ForeignKey('lessons.id'), nullable=False)
    video_id = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(200))
    order = db.Column(db.Integer, default=0)

def search_videos_by_topic(topic, max_results=5):
    """Ищет образовательные видео на YouTube по теме и возвращает список {id, title}."""
    search_url = "https://www.googleapis.com/youtube/v3/search"
    # Добавляем ключевые слова для уточнения поиска
    query = f"{topic} урок объяснение"
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": max_results * 2,  # запрашиваем с запасом, чтобы потом отфильтровать
        "key": API_KEY,
        "relevanceLanguage": "ru",
        "regionCode": "RU",
        "videoDuration": "medium",
        "order": "relevance",
        "videoCategoryId": "27",  # образование
        "safeSearch": "strict"
    }
    try:
        r = requests.get(search_url, params=params)
        data = r.json()
        videos = []
        # Чёрный список слов в названиях (можно дополнить)
        blacklist = [
            'клип', 'песня', 'official', 'video', 'song', 'kids',
            'number two', 'jackass', 'bebefinn', 'remix', 'dance',
            'мультик', 'детский', 'прикол', 'смешной', 'игра'
        ]
        for item in data.get("items", []):
            title = item["snippet"]["title"].lower()
            # Пропускаем, если в названии есть стоп-слова
            if any(word in title for word in blacklist):
                continue
            videos.append({
                "id": item["id"]["videoId"],
                "title": item["snippet"]["title"]
            })
            if len(videos) >= max_results:
                break
        return videos
    except Exception as e:
        print(f"Error searching videos for topic '{topic}': {e}")
        return []

def get_stream(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'noplaylist': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if 'formats' in info:
                for f in info['formats']:
                    if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                        return f['url']
            return info['url']
    except Exception as e:
        print(f"Error in get_stream for {video_id}: {str(e)}")
        return None

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['password'] == ADMIN_PASSWORD:
            session['admin'] = True
            return redirect(url_for('admin'))
        else:
            flash('Неверный пароль')
    return '''
        <form method="post">
            Пароль: <input type="password" name="password">
            <input type="submit" value="Войти">
        </form>
    '''

@app.route('/logout')
def logout():
    session.pop('admin', None)
    return redirect(url_for('home'))

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if not session.get('admin'):
        return redirect(url_for('login'))

    days = Day.query.order_by(Day.order).all()

    if request.method == 'POST':
        day_id = request.form['day_id']
        lesson_number = request.form['lesson_number']
        week = request.form.get('week', 1, type=int)

        existing = Lesson.query.filter_by(
            day_id=day_id,
            lesson_number=lesson_number,
            week=week
        ).first()
        if existing:
            flash('Урок с таким номером на этой неделе уже существует')
            return redirect(url_for('admin'))

        subject = request.form['subject']
        topic = request.form['topic']
        homework = request.form['homework']

        lesson = Lesson(
            day_id=day_id,
            week=week,
            lesson_number=lesson_number,
            subject=subject,
            topic=topic,
            homework=homework
        )
        db.session.add(lesson)
        db.session.flush()

        videos = search_videos_by_topic(topic)
        for order, vid in enumerate(videos, start=1):
            video = Video(
                lesson_id=lesson.id,
                video_id=vid['id'],
                title=vid['title'],
                order=order
            )
            db.session.add(video)

        db.session.commit()
        flash('Урок добавлен с видео по теме')
        return redirect(url_for('admin'))

    lessons = Lesson.query.order_by(Lesson.week, Lesson.day_id, Lesson.lesson_number).all()
    return render_template('admin.html', days=days, lessons=lessons)

@app.route('/quick_add', methods=['POST'])
def quick_add():
    if not session.get('admin'):
        return redirect(url_for('login'))

    day_id = request.form['day_id']
    lesson_number = request.form['lesson_number']
    subject = request.form['subject']
    topic = request.form['topic']
    homework = request.form['homework']
    week = request.form.get('week', 1, type=int)

    existing = Lesson.query.filter_by(day_id=day_id, lesson_number=lesson_number, week=week).first()
    if existing:
        flash('Урок с таким номером на этой неделе уже существует')
        return redirect(url_for('home', week=week))

    lesson = Lesson(
        day_id=day_id,
        week=week,
        lesson_number=lesson_number,
        subject=subject,
        topic=topic,
        homework=homework
    )
    db.session.add(lesson)
    db.session.flush()

    videos = search_videos_by_topic(topic)
    for order, vid in enumerate(videos, start=1):
        video = Video(
            lesson_id=lesson.id,
            video_id=vid['id'],
            title=vid['title'],
            order=order
        )
        db.session.add(video)

    db.session.commit()
    flash('Урок добавлен')
    return redirect(url_for('home', week=week))

@app.route('/edit/<int:id>', methods=['GET', 'POST'])
def edit_lesson(id):
    if not session.get('admin'):
        return redirect(url_for('login'))

    lesson = Lesson.query.get_or_404(id)
    days = Day.query.order_by(Day.order).all()

    if request.method == 'POST':
        day_id = request.form['day_id']
        lesson_number = request.form['lesson_number']
        week = request.form.get('week', 1, type=int)

        existing = Lesson.query.filter(
            Lesson.day_id == day_id,
            Lesson.lesson_number == lesson_number,
            Lesson.week == week,
            Lesson.id != id
        ).first()
        if existing:
            flash('Урок с таким номером на этой неделе уже существует')
            return redirect(url_for('edit_lesson', id=id))

        lesson.day_id = day_id
        lesson.week = week
        lesson.lesson_number = lesson_number
        lesson.subject = request.form['subject']
        lesson.topic = request.form['topic']
        lesson.homework = request.form['homework']

        Video.query.filter_by(lesson_id=lesson.id).delete()
        videos = search_videos_by_topic(lesson.topic)
        for order, vid in enumerate(videos, start=1):
            video = Video(
                lesson_id=lesson.id,
                video_id=vid['id'],
                title=vid['title'],
                order=order
            )
            db.session.add(video)

        db.session.commit()
        flash('Урок обновлён')
        return redirect(url_for('home', week=lesson.week))

    return render_template('edit_lesson.html', lesson=lesson, days=days)

@app.route('/delete/<int:id>')
def delete_lesson(id):
    if not session.get('admin'):
        return redirect(url_for('login'))
    lesson = Lesson.query.get_or_404(id)
    week = lesson.week
    db.session.delete(lesson)
    db.session.commit()
    flash('Урок удалён')
    return redirect(url_for('home', week=week))

@app.route('/lesson/<int:id>')
def lesson_detail(id):
    lesson = Lesson.query.get_or_404(id)
    return render_template('lesson.html', lesson=lesson)

@app.route('/proxy/<video_id>')
def proxy_video(video_id):
    video_url = get_stream(video_id)
    if not video_url:
        return "Video not available", 404

    def generate():
        with requests.get(video_url, stream=True) as r:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

    return Response(stream_with_context(generate()), content_type='video/mp4')

def week_from_date(target_date):
    monday = target_date - timedelta(days=target_date.weekday())
    delta = monday - START_DATE
    if delta.days < 0:
        return 1
    return (delta.days // 7) + 1

MONTHS_RU = {
    1: 'Январь', 2: 'Февраль', 3: 'Март', 4: 'Апрель',
    5: 'Май', 6: 'Июнь', 7: 'Июль', 8: 'Август',
    9: 'Сентябрь', 10: 'Октябрь', 11: 'Ноябрь', 12: 'Декабрь'
}

@app.route("/")
def home():
    date_str = request.args.get('date')
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            week = week_from_date(target_date)
            return redirect(url_for('home', week=week))
        except ValueError:
            pass

    week = request.args.get('week', type=int)
    if week is None:
        today = date.today()
        week = week_from_date(today)
        return redirect(url_for('home', week=week))

    if week < 1:
        week = 1

    first_day_date = START_DATE + timedelta(weeks=week-1, days=0)
    month_name_ru = MONTHS_RU.get(first_day_date.month, '')
    month_year = f"{month_name_ru} {first_day_date.year}"
    current_month = first_day_date.month
    current_year = first_day_date.year

    days = Day.query.order_by(Day.order).all()
    schedule = []
    for i, day in enumerate(days):
        day_date = START_DATE + timedelta(weeks=week-1, days=i)
        lessons = Lesson.query.filter_by(day_id=day.id, week=week).order_by(Lesson.lesson_number).all()
        schedule.append({
    	    'id': day.id,               # ← добавляем id дня
    	    'name': day.name,
   	    'date': day_date,
    	    'lessons': lessons
	})
    today_str = date.today().isoformat()
    is_admin = session.get('admin', False)

    return render_template("index.html",
                           schedule=schedule,
                           current_week=week,
                           month_year=month_year,
                           current_month=current_month,
                           current_year=current_year,
                           today_str=today_str,
                           is_admin=is_admin)

@app.route('/api/lesson/<int:id>')
def api_lesson(id):
    if not session.get('admin'):
        return {"error": "Unauthorized"}, 403
    lesson = Lesson.query.get_or_404(id)
    return {
        "id": lesson.id,
        "day_id": lesson.day_id,
        "week": lesson.week,
        "lesson_number": lesson.lesson_number,
        "subject": lesson.subject,
        "topic": lesson.topic,
        "homework": lesson.homework
    }

@app.route('/copy_week', methods=['POST'])
def copy_week():
    if not session.get('admin'):
        return redirect(url_for('login'))

    source_week = int(request.form['source_week'])
    source_lessons = Lesson.query.filter_by(week=source_week).all()

    if not source_lessons:
        flash('На исходной неделе нет уроков для копирования')
        return redirect(url_for('admin'))

    # Задаём максимальную неделю (можно изменить под свою учебную программу)
    MAX_WEEK = 52
    target_weeks = [w for w in range(1, MAX_WEEK + 1) if w != source_week]

    for week in target_weeks:
        # Удаляем все старые уроки на целевой неделе
        Lesson.query.filter_by(week=week).delete()

        # Копируем каждый урок
        for lesson in source_lessons:
            new_lesson = Lesson(
                day_id=lesson.day_id,
                week=week,
                lesson_number=lesson.lesson_number,
                subject=lesson.subject,
                topic=lesson.topic,
                homework=lesson.homework
            )
            db.session.add(new_lesson)
            db.session.flush()  # получаем id нового урока

            # Копируем связанные видео
            for video in lesson.videos:
                new_video = Video(
                    lesson_id=new_lesson.id,
                    video_id=video.video_id,
                    title=video.title,
                    order=video.order
                )
                db.session.add(new_video)

    db.session.commit()
    flash(f'Расписание успешно скопировано с недели {source_week} на все остальные недели (1–{MAX_WEEK})')
    return redirect(url_for('admin'))

@app.route('/bulk_delete', methods=['POST'])
def bulk_delete():
    if not session.get('admin'):
        return redirect(url_for('login'))
    lesson_ids = request.form.getlist('lesson_ids[]')
    if lesson_ids:
        Lesson.query.filter(Lesson.id.in_(lesson_ids)).delete(synchronize_session=False)
        db.session.commit()
        flash(f'Удалено {len(lesson_ids)} уроков')
    else:
        flash('Ничего не выбрано')
    return redirect(url_for('admin'))

@app.route('/bulk_edit', methods=['POST'])
def bulk_edit():
    if not session.get('admin'):
        return redirect(url_for('login'))
    lesson_ids = request.form.getlist('lesson_ids[]')
    if not lesson_ids:
        flash('Ничего не выбрано')
        return redirect(url_for('admin'))
    subject = request.form.get('subject')
    topic = request.form.get('topic')
    homework = request.form.get('homework')

    lessons = Lesson.query.filter(Lesson.id.in_(lesson_ids)).all()
    for lesson in lessons:
        if subject:
            lesson.subject = subject
        if topic:
            lesson.topic = topic
        if homework is not None:
            lesson.homework = homework
        # Опционально: если нужно обновить видео (по новой теме), можно добавить логику,
        # но оставим пока без автоматического обновления видео при массовом редактировании.
    db.session.commit()
    flash(f'Обновлено {len(lessons)} уроков')
    return redirect(url_for('admin'))

def get_stream_qualities(video_id):
    """Возвращает словарь {разрешение: url} для доступных mp4 потоков с видео и аудио."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        'quiet': True,
        'noplaylist': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            qualities = {}
            # Проходим по всем форматам
            for f in info.get('formats', []):
                # Нам нужны только mp4, содержащие и видео, и аудио
                if (f.get('ext') == 'mp4' and
                    f.get('vcodec') != 'none' and
                    f.get('acodec') != 'none'):
                    height = f.get('height')
                    if height:
                        qualities[str(height)] = f['url']
            # Если не нашли ни одного подходящего формата, добавим запасной URL (может быть не mp4)
            if not qualities and info.get('url'):
                qualities['best'] = info['url']
            return qualities
    except Exception as e:
        print(f"Error in get_stream_qualities for {video_id}: {str(e)}")
        return {}

@app.route('/api/qualities/<video_id>')
def api_qualities(video_id):
    """Возвращает JSON со списком доступных качеств (только идентификатор видео, без прямых URL)."""
    qualities = get_stream_qualities(video_id)
    # Преобразуем: оставляем только ключи (разрешения), значения теперь не нужны
    available_qualities = list(qualities.keys())
    return {'video_id': video_id, 'qualities': available_qualities}

@app.route('/admin/import_json', methods=['GET', 'POST'])
def import_json():
    if not session.get('admin'):
        return redirect(url_for('login'))

    if request.method == 'POST':
        json_data = request.form.get('json_data')
        if not json_data:
            flash('Нет данных для импорта')
            return redirect(url_for('import_json'))

        try:
            data = json.loads(json_data)
        except json.JSONDecodeError:
            flash('Неверный формат JSON')
            return redirect(url_for('import_json'))

        # Удаляем все существующие уроки и видео (полная замена)
        try:
            # Сначала удаляем видео, потом уроки (из-за внешних ключей)
            Video.query.delete()
            Lesson.query.delete()
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка при очистке базы: {str(e)}')
            return redirect(url_for('import_json'))

        # Сопоставление дат с днями недели (id дней)
        # Дни недели уже должны быть в БД: Понедельник=1, Вторник=2, ..., Суббота=6
        days_map = {
            '2026-03-16': 1,  # понедельник
            '2026-03-17': 2,  # вторник
            '2026-03-18': 3,  # среда
            '2026-03-19': 4,  # четверг
            '2026-03-20': 5,  # пятница
            # суббота отсутствует, можно добавить позже
        }

        # Для каждой даты (дня) из JSON
        for date_str, lessons_dict in data.items():
            if date_str not in days_map:
                print(f"Дата {date_str} не сопоставлена дню недели, пропускаем")
                continue
            day_id = days_map[date_str]

            # lessons_dict — объект с ключами-номерами уроков
            for lesson_num_str, lesson_data in lessons_dict.items():
                try:
                    lesson_num = int(lesson_num_str)
                except ValueError:
                    continue  # пропускаем нечисловые ключи

                # Получаем данные урока
                subject = lesson_data.get('discipline', '')
                topic = lesson_data.get('subject', '')
                homework = lesson_data.get('homework', '')

                # Создаём урок для каждой недели (1..52)
                for week in range(1, 53):
                    lesson = Lesson(
                        day_id=day_id,
                        week=week,
                        lesson_number=lesson_num,
                        subject=subject,
                        topic=topic,
                        homework=homework
                    )
                    db.session.add(lesson)
                    db.session.flush()  # чтобы получить id урока

                    # Автоматически подбираем видео по теме
                    if topic:
                        videos = search_videos_by_topic(topic)
                        for order, vid in enumerate(videos, start=1):
                            video = Video(
                                lesson_id=lesson.id,
                                video_id=vid['id'],
                                title=vid['title'],
                                order=order
                            )
                            db.session.add(video)

        try:
            db.session.commit()
            flash(f'Расписание успешно импортировано на все недели (1-52)')
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка при сохранении: {str(e)}')

        return redirect(url_for('admin'))

    # GET запрос — показываем форму
    return '''
        <h2>Импорт расписания из JSON</h2>
        <form method="post">
            <textarea name="json_data" rows="20" cols="80" placeholder="Вставьте JSON с расписанием"></textarea><br>
            <button type="submit">Импортировать (заменит все существующие уроки!)</button>
        </form>
        <p><a href="''' + url_for('admin') + '''">Назад в админку</a></p>
    '''
def import_schedule_data(data, week):
    """Общая логика импорта словаря расписания на указанную неделю."""
    # Удаляем старые уроки на этой неделе
    Lesson.query.filter_by(week=week).delete()
    db.session.commit()

    days_map = {day.name: day.id for day in Day.query.all()}
    created = 0

    for date_str, lessons in data.items():
        if not isinstance(lessons, dict):
            continue
        try:
            dt = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            continue
        weekday = dt.weekday()
        if weekday >= 5:  # суббота, воскресенье – пропускаем
            continue
        day_name = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница'][weekday]
        day_id = days_map.get(day_name)
        if not day_id:
            continue

        for lesson_num_str, lesson_info in lessons.items():
            if not isinstance(lesson_info, dict):
                continue
            lesson_num = int(lesson_num_str)
            discipline = lesson_info.get('discipline', '')
            topic = lesson_info.get('subject', '')
            homework = lesson_info.get('homework', '')
            if not discipline:
                continue

            lesson = Lesson(
                day_id=day_id,
                week=week,
                lesson_number=lesson_num,
                subject=discipline,
                topic=topic,
                homework=homework
            )
            db.session.add(lesson)
            db.session.flush()

            # Автоматический подбор видео
            videos = search_videos_by_topic(topic)
            for order, vid in enumerate(videos, start=1):
                video = Video(
                    lesson_id=lesson.id,
                    video_id=vid['id'],
                    title=vid['title'],
                    order=order
                )
                db.session.add(video)
            created += 1

    db.session.commit()
    return created

@app.route('/import_har', methods=['POST'])
def import_har():
    if not session.get('admin'):
        return redirect(url_for('login'))

    if 'har_file' not in request.files:
        flash('Файл HAR не загружен')
        return redirect(url_for('admin'))

    file = request.files['har_file']
    if file.filename == '':
        flash('Файл не выбран')
        return redirect(url_for('admin'))

    week = request.form.get('week', type=int)
    if not week:
        flash('Не указана неделя')
        return redirect(url_for('admin'))

    try:
        har_data = json.load(file)
    except Exception as e:
        flash(f'Ошибка парсинга HAR-файла: {e}')
        return redirect(url_for('admin'))

    # Ищем нужный entry в HAR
    found = False
    for entry in har_data.get('log', {}).get('entries', []):
        url = entry.get('request', {}).get('url', '')
        if not url.startswith('https://api.in-shkola.ru'):
            continue

        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if 'student_id' not in query or 'klass_id' not in query:
            continue

        response = entry.get('response', {})
        content = response.get('content', {})
        text = content.get('text')
        if text is None:
            continue

        try:
            schedule_data = json.loads(text)
            found = True
            break
        except json.JSONDecodeError:
            continue

    if not found:
        flash('В HAR-файле не найден подходящий запрос с расписанием.')
        return redirect(url_for('admin'))

    # Импортируем данные
    try:
        created = import_schedule_data(schedule_data, week)
        flash(f'Импортировано {created} уроков на неделю {week}')
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при импорте: {e}')

    return redirect(url_for('admin'))

if __name__ == "__main__":
    app.run()
