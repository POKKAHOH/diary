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

from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(app.root_path, 'school.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB
db = SQLAlchemy(app)

# ---------- Конфигурация из .env ----------
API_KEY = os.getenv("API_KEY")
app.secret_key = os.getenv("app.secret_key")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
YTDLP_PROXY_COOKIEFILE = os.getenv("YTDLP_PROXY_COOKIEFILE", "/var/www/flaskapp/cookies.txt")
YTDLP_PLAYER_CLIENTS = [client.strip() for client in os.getenv("YTDLP_PLAYER_CLIENTS", "default").split(",") if client.strip()]
YTDLP_PO_TOKEN = os.getenv("YTDLP_PO_TOKEN")
YTDLP_VISITOR_DATA = os.getenv("YTDLP_VISITOR_DATA")
UPSTREAM_TIMEOUT = (10, 60)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
START_DATE = date(2025, 9, 1)  # 1 сентября 2025 – понедельник первой недели

# Русская локаль для месяцев
try:
    locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
except:
    pass

# ---------- Модели ----------
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
    generated_text_short = db.Column(db.Text, default='')
    generated_text_medium = db.Column(db.Text, default='')
    generated_text_long = db.Column(db.Text, default='')
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

class Subject(db.Model):
    __tablename__ = 'subjects'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)

# ---------- Функции для внешних API ----------
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

def build_embed_fallback(video_id):
    return f'''
    <!DOCTYPE html>
    <html>
    <head><title>Р’РёРґРµРѕ</title></head>
    <body style="background:#0f0f0f; display:flex; justify-content:center; align-items:center; height:100vh;">
        <iframe width="800" height="450"
                src="https://www.youtube.com/embed/{video_id}?autoplay=1&rel=0"
                frameborder="0" allow="autoplay; encrypted-media" allowfullscreen>
        </iframe>
    </body>
    </html>
    '''

def build_ytdlp_options():
    youtube_args = {
        'player_client': YTDLP_PLAYER_CLIENTS or ['default'],
    }
    if YTDLP_PO_TOKEN:
        youtube_args['po_token'] = [YTDLP_PO_TOKEN]
    if YTDLP_VISITOR_DATA:
        youtube_args['visitor_data'] = [YTDLP_VISITOR_DATA]

    ydl_opts = {
        'format': 'best[protocol=https][vcodec!=none][acodec!=none][ext=mp4]/best[protocol=https][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]',
        'quiet': True,
        'noplaylist': True,
        'extractor_args': {
            'youtube': youtube_args,
        },
        'http_headers': {
            'Referer': 'https://www.youtube.com/',
            'Origin': 'https://www.youtube.com',
        },
    }
    if os.path.exists(YTDLP_PROXY_COOKIEFILE):
        ydl_opts['cookiefile'] = YTDLP_PROXY_COOKIEFILE
    return ydl_opts

def select_progressive_format(info):
    formats = info.get('formats') or []
    candidates = []
    for stream in formats:
        if not stream.get('url'):
            continue
        if stream.get('vcodec') in (None, 'none') or stream.get('acodec') in (None, 'none'):
            continue
        if stream.get('protocol') not in ('http', 'https'):
            continue
        candidates.append(stream)
    if not candidates and info.get('url') and info.get('protocol') in ('http', 'https'):
        return info
    if not candidates:
        return None
    return max(candidates, key=lambda item: ((item.get('height') or 0), (item.get('tbr') or 0)))

def normalize_upstream_headers(*header_sources):
    headers = {}
    for header_source in header_sources:
        if not header_source:
            continue
        for key, value in header_source.items():
            headers[key] = value
    headers.setdefault('Referer', 'https://www.youtube.com/')
    headers.setdefault('Origin', 'https://www.youtube.com')
    return headers

def get_stream(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'quiet': True,
        'noplaylist': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['web'],   # или ['android'] – пробуйте оба
            }
        }
    }
    # Если есть cookies.txt, подключаем
    cookies_path = '/var/www/flaskapp/cookies.txt'
    if os.path.exists(cookies_path):
        ydl_opts['cookiefile'] = cookies_path
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

def resolve_stream(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(build_ytdlp_options()) as ydl:
            info = ydl.extract_info(url, download=False)
            selected_format = select_progressive_format(info)
            if not selected_format:
                return None
            return {
                'url': selected_format.get('url'),
                'headers': normalize_upstream_headers(
                    info.get('http_headers'),
                    selected_format.get('http_headers'),
                ),
                'content_type': selected_format.get('ext') or info.get('ext') or 'mp4',
            }
    except Exception as e:
        print(f"Error in resolve_stream for {video_id}: {str(e)}")
        return None

def week_from_date(target_date):
    monday = target_date - timedelta(days=target_date.weekday())
    delta = monday - START_DATE
    if delta.days < 0:
        return 1
    return (delta.days // 7) + 1

def generate_lesson_text_with_length(topic, length):
    if not OPENROUTER_API_KEY:
        return "Текст не сгенерирован: отсутствует API-ключ OpenRouter."
    if length == 'short':
        prompt = f"Напиши очень краткий текст на тему '{topic}' для школьника 10 класса. Объясни основные понятия простым языком. 4-5 предложений."
        max_tokens = 400
    elif length == 'long':
        prompt = f"Напиши очень подробный и объёмный текст на тему '{topic}' для школьника 10 класса. Полностью раскрой тему, объясни все важные аспекты. Не ограничивай себя в количестве предложений."
        max_tokens = 2000
    else:
        prompt = f"Напиши средний по объёму текст на тему '{topic}' для школьника 10 класса. Объясни тему достаточно подробно, около 10 предложений."
        max_tokens = 700
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "openai/gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7
    }
    try:
        response = requests.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        return data['choices'][0]['message']['content']
    except Exception as e:
        print(f"Ошибка генерации текста: {e}")
        return "Не удалось сгенерировать текст. Попробуйте позже."

def get_or_generate_lesson_text(lesson, length):
    if length == 'short':
        if lesson.generated_text_short:
            return lesson.generated_text_short
    elif length == 'long':
        if lesson.generated_text_long:
            return lesson.generated_text_long
    else:
        if lesson.generated_text_medium:
            return lesson.generated_text_medium
    new_text = generate_lesson_text_with_length(lesson.topic, length)
    if new_text and not new_text.startswith("Не удалось"):
        if length == 'short':
            lesson.generated_text_short = new_text
        elif length == 'long':
            lesson.generated_text_long = new_text
        else:
            lesson.generated_text_medium = new_text
        db.session.commit()
    return new_text

def import_schedule_data(data, week, skip_videos=False):
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
            discipline = lesson_info.get('discipline')
            if not discipline:
                continue
            topic = lesson_info.get('subject', '')
            homework = lesson_info.get('homework', '')
            # Добавляем предмет в справочник, если его нет
            if not Subject.query.filter_by(name=discipline).first():
                db.session.add(Subject(name=discipline))
            lesson = Lesson(
                day_id=day_id, week=week, lesson_number=lesson_num,
                subject=discipline, topic=topic, homework=homework
            )
            db.session.add(lesson)
            db.session.flush()
            if not skip_videos:
                videos = search_videos_by_topic(topic)
                for order, vid in enumerate(videos, start=1):
                    db.session.add(Video(lesson_id=lesson.id, video_id=vid['id'], title=vid['title'], order=order))
            created += 1
    db.session.commit()
    return created

# ---------- Маршруты ----------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['password'] == ADMIN_PASSWORD:
            session['admin'] = True
            return redirect(url_for('admin'))
        else:
            flash('Неверный пароль')
            return redirect(url_for('login'))
    return render_template('login.html')

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
        # Добавляем предмет, если его нет
        if not Subject.query.filter_by(name=subject).first():
            db.session.add(Subject(name=subject))
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
    subjects = Subject.query.order_by(Subject.name).all()
    return render_template('admin.html', days=days, lessons=lessons, subjects=subjects)

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
    if not Subject.query.filter_by(name=subject).first():
        db.session.add(Subject(name=subject))
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
    subjects = Subject.query.order_by(Subject.name).all()
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
        # Обновляем поля
        old_topic = lesson.topic
        lesson.day_id = day_id
        lesson.week = week
        lesson.lesson_number = lesson_number
        lesson.subject = request.form['subject']
        lesson.topic = request.form['topic']
        lesson.homework = request.form['homework']
        # Если тема изменилась – сбрасываем кэш текстов и обновляем видео
        if old_topic != lesson.topic:
            lesson.generated_text_short = ''
            lesson.generated_text_medium = ''
            lesson.generated_text_long = ''
            Video.query.filter_by(lesson_id=lesson.id).delete()
            videos = search_videos_by_topic(lesson.topic)
            for order, vid in enumerate(videos, start=1):
                db.session.add(Video(lesson_id=lesson.id, video_id=vid['id'], title=vid['title'], order=order))
        db.session.commit()
        flash('Урок обновлён')
        return redirect(url_for('home', week=lesson.week))
    return render_template('edit_lesson.html', lesson=lesson, days=days, subjects=subjects)

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
    # Генерация видео, если их нет
    if not lesson.videos:
        videos = search_videos_by_topic(lesson.topic)
        if videos:
            for order, vid in enumerate(videos, start=1):
                db.session.add(Video(lesson_id=lesson.id, video_id=vid['id'], title=vid['title'], order=order))
            db.session.commit()
            lesson = Lesson.query.get_or_404(id)
    # Получение текста (с кэшированием)
    length = request.args.get('length', 'medium')
    if length not in ['short', 'medium', 'long']:
        length = 'medium'
    generated_text = get_or_generate_lesson_text(lesson, length)
    return render_template('lesson.html', lesson=lesson, week=lesson.week,
                           generated_text=generated_text, current_length=length)

@app.route('/proxy/<video_id>', methods=['GET', 'HEAD'])
def proxy_video(video_id):
    stream = resolve_stream(video_id)
    if stream:
        upstream_headers = dict(stream.get('headers') or {})
        range_header = request.headers.get('Range')
        if range_header:
            upstream_headers['Range'] = range_header
        upstream_headers.setdefault('User-Agent', request.headers.get('User-Agent', 'Mozilla/5.0'))

        try:
            upstream = requests.request(
                request.method,
                stream['url'],
                headers=upstream_headers,
                stream=(request.method != 'HEAD'),
                allow_redirects=True,
                timeout=UPSTREAM_TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"Error proxying {video_id}: {e}")
            return build_embed_fallback(video_id)

        if upstream.status_code >= 400:
            print(f"Upstream rejected {video_id} with status {upstream.status_code}")
            upstream.close()
            return build_embed_fallback(video_id)

        response_headers = {}
        for key, value in upstream.headers.items():
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            response_headers[key] = value
        response_headers.setdefault('Accept-Ranges', 'bytes')
        response_headers.setdefault('Content-Type', f"video/{stream.get('content_type', 'mp4')}")

        if request.method == 'HEAD':
            upstream.close()
            return Response(status=upstream.status_code, headers=response_headers)

        def generate_upstream():
            try:
                for chunk in upstream.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return Response(
            stream_with_context(generate_upstream()),
            status=upstream.status_code,
            headers=response_headers,
            direct_passthrough=True,
        )
    return build_embed_fallback(video_id)
    video_url = get_stream(video_id)
    if not video_url:
        # fallback: показываем iframe, если прокси не сработал
        return f'''
        <!DOCTYPE html>
        <html>
        <head><title>Видео</title></head>
        <body style="background:#0f0f0f; display: flex; justify-content: center; align-items: center; height: 100vh;">
            <iframe width="800" height="450" 
                    src="https://www.youtube.com/embed/{video_id}" 
                    frameborder="0" allowfullscreen>
            </iframe>
        </body>
        </html>
        '''
    def generate():
        headers = {}
        range_header = request.headers.get('Range', None)
        if range_header:
            headers['Range'] = range_header
        with requests.get(video_url, stream=True, headers=headers) as r:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
    response = Response(stream_with_context(generate()), content_type='video/mp4')
    # Проксируем заголовки для перемотки
    if 'Content-Range' in response.headers:
        response.headers['Content-Range'] = response.headers['Content-Range']
    if 'Accept-Ranges' in response.headers:
        response.headers['Accept-Ranges'] = response.headers['Accept-Ranges']
    response.headers['Accept-Ranges'] = 'bytes'
    return response

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
