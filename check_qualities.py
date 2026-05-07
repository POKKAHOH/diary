import yt_dlp

ydl_opts = {'format': 'bestvideo+bestaudio/best', 'quiet': True}
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    info = ydl.extract_info('https://www.youtube.com/watch?v=dQw4w9WgXcQ', download=False)
    heights = [f.get('height') for f in info.get('formats', []) if f.get('height')]
    print('Доступные высоты:', sorted(set(heights)))
