from flask import Flask, render_template, request, Response, stream_with_context, session, redirect, url_for, flash, abort
from datetime import date, timedelta, datetime
import locale
import requests
import yt_dlp
from flask_sqlalchemy import SQLAlchemy
import os
import json
from urllib.parse import urlparse, parse_qs
import re
import sys

from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(app.root_path, 'school.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB
db = SQLAlchemy(app)

API_KEY = os.getenv("API_KEY")
app.secret_key = os.getenv("app.secret_key")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
START_DATE = date(2025, 9, 1)

# OpenRouter
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

try:
    locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
except:
    pass

# ------------------ МОДЕЛИ ------------------
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
    generated_text = db.Column(db.Text, default='')
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

# ------------------ ФУНКЦИИ ------------------
def search_videos_by_topic(topic, max_results=5):
    if not API_KEY:
        return []
    search_url = "https://www.googleapis.com/youtube/v3/search"
    query = f"{topic} урок объяснение"
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": max_results,
        "key": API_KEY,
        "relevanceLanguage": "ru",
        "regionCode": "RU",
        "videoDuration": "medium",
        "videoCategoryId": "27",
        "safeSearch": "strict"
    }
    try:
        r = requests.get(search_url, params=params, timeout=10)
        data = r.json()
        videos = []
        for item in data.get("items", []):
            title = item["snippet"]["title"]
            if len(title) < 3:
                continue
            videos.append({
                "id": item["id"]["videoId"],
                "title": title
            })
        return videos[:max_results]
    except Exception as e:
        print(f"Error searching videos for topic '{topic}': {e}")
        return []

def get_stream(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        'format': 'best[ext=mp4]/best',
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
            return info.get('url')
    except Exception as e:
        print(f"Error in get_stream for {video_id}: {str(e)}")
        return None

def week_from_date(target_date):
    monday = target_date - timedelta(days=target_date.weekday())
    delta = monday - START_DATE
    if delta.days < 0:
        return 1
    return (delta.days // 7) + 1

def generate_lesson_text(topic):
    if not OPENROUTER_API_KEY:
        return "Текст не сгенерирован: отсутствует API-ключ OpenRouter."
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = f"Напиши краткий образовательный текст на тему '{topic}' для школьника 10 класса. Объясни основные понятия простым языком, приведи примеры, подбери картинки. Объём 14-20 предложений. "
    payload = {
        "model": "openai/gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "temperature": 0.7
    }
    try:
        response = requests.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        return data['choices'][0]['message']['content']
    except Exception as e:
        print(f"Ошибка генерации текста: {e}")
        return "Не удалось сгенерировать текст. Попробуйте позже."

def import_schedule_data(data, week, skip_videos=False):
    """Импорт расписания из JSON. Если skip_videos=True, видео не подбираются."""
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
        if weekday >= 5:
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

            if not skip_videos:
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

# ------------------ МАРШРУТЫ ------------------
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
        existing = Lesson.query.filter_by(day_id=day_id, lesson_number=lesson_number, week=week).first()
        if existing:
            flash('Урок с таким номером на этой неделе уже существует')
            return redirect(url_for('admin'))
        subject = request.form['subject']
        topic = request.form['topic']
        homework = request.form['homework']
        lesson = Lesson(day_id=day_id, week=week, lesson_number=lesson_number,
                        subject=subject, topic=topic, homework=homework)
        db.session.add(lesson)
        db.session.flush()
        videos = search_videos_by_topic(topic)
        for order, vid in enumerate(videos, start=1):
            db.session.add(Video(lesson_id=lesson.id, video_id=vid['id'], title=vid['title'], order=order))
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
    lesson = Lesson(day_id=day_id, week=week, lesson_number=lesson_number,
                    subject=subject, topic=topic, homework=homework)
    db.session.add(lesson)
    db.session.flush()
    videos = search_videos_by_topic(topic)
    for order, vid in enumerate(videos, start=1):
        db.session.add(Video(lesson_id=lesson.id, video_id=vid['id'], title=vid['title'], order=order))
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
        existing = Lesson.query.filter(Lesson.day_id == day_id, Lesson.lesson_number == lesson_number,
                                        Lesson.week == week, Lesson.id != id).first()
        if existing:
            flash('Урок с таким номером на этой неделе уже существует')
            return redirect(url_for('edit_lesson', id=id))
        lesson.day_id = day_id
        lesson.week = week
        lesson.lesson_number = lesson_number
        lesson.subject = request.form['subject']
        lesson.topic = request.form['topic']
        lesson.homework = request.form['homework']
        # Не меняем generated_text и видео при редактировании темы? По желанию.
        Video.query.filter_by(lesson_id=lesson.id).delete()
        videos = search_videos_by_topic(lesson.topic)
        for order, vid in enumerate(videos, start=1):
            db.session.add(Video(lesson_id=lesson.id, video_id=vid['id'], title=vid['title'], order=order))
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

    # 1. Генерация видео, если их нет
    if not lesson.videos:
        videos = search_videos_by_topic(lesson.topic)
        if videos:
            for order, vid in enumerate(videos, start=1):
                video = Video(
                    lesson_id=lesson.id,
                    video_id=vid['id'],
                    title=vid['title'],
                    order=order
                )
                db.session.add(video)
            db.session.commit()
            lesson = Lesson.query.get_or_404(id)

    # 2. Генерация текста, если его нет
    if not lesson.generated_text:
        text = generate_lesson_text(lesson.topic)
        if text:
            lesson.generated_text = text
            db.session.commit()
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

@app.route('/import_json', methods=['POST'])
def import_json():
    if not session.get('admin'):
        return redirect(url_for('login'))
    if 'json_file' not in request.files:
        flash('Файл JSON не загружен')
        return redirect(url_for('admin'))
    file = request.files['json_file']
    if file.filename == '':
        flash('Файл не выбран')
        return redirect(url_for('admin'))
    # Определяем неделю из имени файла week<number>.json
    week = None
    match = re.search(r'week(\d+)\.json$', file.filename, re.IGNORECASE)
    if match:
        week = int(match.group(1))
    else:
        week = request.form.get('week', type=int)
    if not week:
        flash('Не удалось определить неделю. Назовите файл как week<номер>.json или укажите неделю вручную.')
        return redirect(url_for('admin'))
    try:
        data = json.load(file)
    except Exception as e:
        flash(f'Ошибка парсинга JSON: {e}')
        return redirect(url_for('admin'))
    try:
        created = import_schedule_data(data, week, skip_videos=True)
        flash(f'Импортировано {created} уроков на неделю {week}')
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при импорте: {e}')
    return redirect(url_for('admin'))

@app.route('/copy_week', methods=['POST'])
def copy_week():
    if not session.get('admin'):
        return redirect(url_for('login'))
    source_week = int(request.form['source_week'])
    source_lessons = Lesson.query.filter_by(week=source_week).all()
    if not source_lessons:
        flash('На исходной неделе нет уроков для копирования')
        return redirect(url_for('admin'))
    MAX_WEEK = 52
    target_weeks = [w for w in range(1, MAX_WEEK + 1) if w != source_week]
    for week in target_weeks:
        Lesson.query.filter_by(week=week).delete()
        for lesson in source_lessons:
            new_lesson = Lesson(
                day_id=lesson.day_id, week=week, lesson_number=lesson.lesson_number,
                subject=lesson.subject, topic=lesson.topic, homework=lesson.homework
            )
            db.session.add(new_lesson)
            db.session.flush()
            for video in lesson.videos:
                db.session.add(Video(lesson_id=new_lesson.id, video_id=video.video_id,
                                     title=video.title, order=video.order))
    db.session.commit()
    flash(f'Расписание скопировано с недели {source_week} на все остальные недели (1–{MAX_WEEK})')
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
    updates = {}
    if subject:
        updates['subject'] = subject
    if topic:
        updates['topic'] = topic
    if homework:
        updates['homework'] = homework
    if not updates:
        flash('Нет данных для обновления')
        return redirect(url_for('admin'))
    Lesson.query.filter(Lesson.id.in_(lesson_ids)).update(updates, synchronize_session=False)
    if topic:
        lessons = Lesson.query.filter(Lesson.id.in_(lesson_ids)).all()
        for lesson in lessons:
            Video.query.filter_by(lesson_id=lesson.id).delete()
            videos = search_videos_by_topic(lesson.topic)
            for order, vid in enumerate(videos, start=1):
                db.session.add(Video(lesson_id=lesson.id, video_id=vid['id'], title=vid['title'], order=order))
    db.session.commit()
    flash(f'Обновлено {len(lesson_ids)} уроков')
    return redirect(url_for('admin'))

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
    month_name_ru = {1: 'Январь',2:'Февраль',3:'Март',4:'Апрель',5:'Май',6:'Июнь',
                     7:'Июль',8:'Август',9:'Сентябрь',10:'Октябрь',11:'Ноябрь',12:'Декабрь'}.get(first_day_date.month, '')
    month_year = f"{month_name_ru} {first_day_date.year}"
    days = Day.query.order_by(Day.order).all()
    schedule = []
    for i, day in enumerate(days):
        day_date = START_DATE + timedelta(weeks=week-1, days=i)
        lessons = Lesson.query.filter_by(day_id=day.id, week=week).order_by(Lesson.lesson_number).all()
        schedule.append({'id': day.id, 'name': day.name, 'date': day_date, 'lessons': lessons})
    today_str = date.today().isoformat()
    is_admin = session.get('admin', False)
    return render_template("index.html",
                           schedule=schedule,
                           current_week=week,
                           month_year=month_year,
                           current_month=first_day_date.month,
                           current_year=first_day_date.year,
                           today_str=today_str,
                           is_admin=is_admin)

if __name__ == "__main__":
    app.run()
