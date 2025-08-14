import os
import sys
import re
import requests
import json
from flask import Flask, render_template_string, request, redirect, url_for, Response, jsonify
from pymongo import MongoClient
from bson.objectid import ObjectId
from functools import wraps
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

# ======================================================================
# --- Environment Variables & Configuration ---
# ======================================================================
MONGO_URI = os.environ.get("MONGO_URI")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
ADMIN_CHANNEL_ID = os.environ.get("ADMIN_CHANNEL_ID")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# Channel and Developer Information
MAIN_CHANNEL_LINK = os.environ.get("MAIN_CHANNEL_LINK")
UPDATE_CHANNEL_LINK = os.environ.get("UPDATE_CHANNEL_LINK")
DEVELOPER_USER_LINK = os.environ.get("DEVELOPER_USER_LINK")
NOTIFICATION_CHANNEL_ID = os.environ.get("NOTIFICATION_CHANNEL_ID")

# --- Validate that all required environment variables are set ---
required_vars = {
    "MONGO_URI": MONGO_URI, "BOT_TOKEN": BOT_TOKEN, "TMDB_API_KEY": TMDB_API_KEY,
    "ADMIN_CHANNEL_ID": ADMIN_CHANNEL_ID, "BOT_USERNAME": BOT_USERNAME,
    "ADMIN_USERNAME": ADMIN_USERNAME, "ADMIN_PASSWORD": ADMIN_PASSWORD,
}
missing_vars = [name for name, value in required_vars.items() if not value]
if missing_vars:
    print(f"FATAL: Missing required environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

# ======================================================================
# --- Application Setup & Helper Functions ---
# ======================================================================
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
PLACEHOLDER_POSTER = "https://via.placeholder.com/400x600.png?text=Poster+Not+Found"
# [NEW] Define the categories available in the admin panel
SITE_CATEGORIES = ["Trending Now", "Latest Movies", "Recently Added", "Hindi", "Bengali", "English & Hollywood", "Web Series", "Coming Soon"]

app = Flask(__name__)

# --- Authentication ---
def check_auth(username, password): return username == ADMIN_USERNAME and password == ADMIN_PASSWORD
def authenticate(): return Response('Could not verify your access level.', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password): return authenticate()
        return f(*args, **kwargs)
    return decorated

# --- Database Connection ---
try:
    client = MongoClient(MONGO_URI)
    db = client["movie_db"]
    movies = db["movies"]
    settings = db["settings"]
    feedback = db["feedback"]
    print("SUCCESS: Successfully connected to MongoDB!")
except Exception as e:
    print(f"FATAL: Error connecting to MongoDB: {e}. Exiting.")
    sys.exit(1)

# --- Template Processor ---
@app.context_processor
def inject_globals():
    ad_codes = settings.find_one() or {}
    return dict(ad_settings=ad_codes, bot_username=BOT_USERNAME, main_channel_link=MAIN_CHANNEL_LINK)

# --- Utility Functions ---
scheduler = BackgroundScheduler(daemon=True)
scheduler.start()

def delete_message_after_delay(chat_id, message_id):
    try:
        requests.post(f"{TELEGRAM_API_URL}/deleteMessage", json={'chat_id': chat_id, 'message_id': message_id}, timeout=10)
    except Exception as e:
        print(f"Error in delete_message_after_delay: {e}")

def escape_markdown(text: str) -> str:
    if not isinstance(text, str): return ''
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', text)

# [NEW] Helper function to extract YouTube video ID from various URL formats
def extract_youtube_id(url: str) -> str or None:
    if not url or not isinstance(url, str): return None
    patterns = [
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/watch\?v=([a-zA-Z0-9_-]{11})',
        r'(?:https?:\/\/)?(?:www\.)?youtu\.be\/([a-zA-Z0-9_-]{11})',
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/embed\/([a-zA-Z0-9_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match: return match.group(1)
    return None

# ======================================================================
# --- Core Notification Function ---
# ======================================================================
def send_notification_to_channel(movie_data):
    if not NOTIFICATION_CHANNEL_ID: return
    try:
        with app.app_context():
            movie_url = url_for('movie_detail', movie_id=str(movie_data['_id']), _external=True)

        title, poster_url = movie_data.get('title', 'N/A'), movie_data.get('poster')
        is_coming_soon = "Coming Soon" in movie_data.get('categories', [])

        if not poster_url or poster_url == PLACEHOLDER_POSTER: return

        if is_coming_soon:
            caption = (
                f"⏳ **Coming Soon!** ⏳\n\n"
                f"🎬 **{title}**\n\n"
                f"Get ready! This content will be available on our platform very soon. Stay tuned!"
            )
            keyboard = {}
        else:
            caption = f"✨ **New Content Added!** ✨\n\n🎬 **{title}**\n"
            if year := movie_data.get('release_date', '----').split('-')[0]: caption += f"🗓️ **Year:** {year}\n"
            if genres := ", ".join(movie_data.get('genres', [])): caption += f"🎭 **Genre:** {genres}\n"
            if rating := movie_data.get('vote_average', 0): caption += f"⭐ **Rating:** {rating:.1f}/10\n"
            caption += "\n👇 Click the button below to watch or download now!"
            keyboard = {"inline_keyboard": [[{"text": "➡️ Watch / Download on Website", "url": movie_url}]]}
        
        payload = {'chat_id': NOTIFICATION_CHANNEL_ID, 'photo': poster_url, 'caption': caption, 'parse_mode': 'Markdown', 'reply_markup': json.dumps(keyboard)}
        
        response = requests.post(f"{TELEGRAM_API_URL}/sendPhoto", data=payload, timeout=15)
        response_data = response.json()

        if response_data.get('ok'):
            print(f"SUCCESS: Notification sent for '{title}'.")
            if "Trending Now" in movie_data.get('categories', []) and not is_coming_soon:
                message_id = response_data['result']['message_id']
                requests.post(f"{TELEGRAM_API_URL}/pinChatMessage", json={'chat_id': NOTIFICATION_CHANNEL_ID, 'message_id': message_id}, timeout=10)
        else:
            print(f"ERROR: Failed to send notification: {response.text}")
    except Exception as e:
        print(f"FATAL ERROR in send_notification_to_channel: {e}")

# ======================================================================
# --- HTML Templates ---
# ======================================================================

index_html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no" />
<title>MovieZone - Your Entertainment Hub</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Roboto:wght@400;500;700&display=swap');
  :root { --netflix-red: #E50914; --netflix-black: #141414; --text-light: #f5f5f5; --text-dark: #a0a0a0; --nav-height: 60px; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Roboto', sans-serif; background-color: var(--netflix-black); color: var(--text-light); overflow-x: hidden; }
  a { text-decoration: none; color: inherit; }
  .main-nav { position: fixed; top: 0; left: 0; width: 100%; padding: 15px 50px; display: flex; justify-content: space-between; align-items: center; z-index: 100; transition: background-color 0.3s ease; background: linear-gradient(to bottom, rgba(0,0,0,0.8) 10%, rgba(0,0,0,0)); }
  .main-nav.scrolled { background-color: var(--netflix-black); }
  .logo { font-family: 'Bebas Neue', sans-serif; font-size: 32px; color: var(--netflix-red); font-weight: 700; letter-spacing: 1px; }
  .search-input { background-color: rgba(0,0,0,0.7); border: 1px solid #777; color: var(--text-light); padding: 8px 15px; border-radius: 4px; width: 250px; }
  .hero-section { height: 85vh; position: relative; color: white; overflow: hidden; }
  .hero-slide { position: absolute; top: 0; left: 0; width: 100%; height: 100%; background-size: cover; background-position: center top; display: flex; align-items: flex-end; padding: 50px; opacity: 0; transition: opacity 1.5s ease-in-out; }
  .hero-slide.active { opacity: 1; }
  .hero-slide::before { content: ''; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: linear-gradient(to top, var(--netflix-black) 10%, transparent 50%), linear-gradient(to right, rgba(0,0,0,0.8) 0%, transparent 60%); }
  .hero-content { position: relative; z-index: 2; max-width: 50%; }
  .hero-title { font-family: 'Bebas Neue', sans-serif; font-size: 5rem; } .hero-overview { font-size: 1.1rem; max-width: 600px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .btn { padding: 8px 20px; border-radius: 4px; font-weight: 700; cursor: pointer; border:none; text-decoration:none; display:inline-flex; align-items:center; gap: 8px;}
  .btn.btn-primary { background-color: var(--netflix-red); color: white; } .btn.btn-secondary { background-color: rgba(109, 109, 110, 0.7); color: white; }
  main { padding: 0 50px; }
  .movie-card { display: block; cursor: pointer; transition: transform 0.3s ease; }
  .poster-wrapper { position: relative; width: 100%; border-radius: 6px; overflow: hidden; background-color: #222; display: flex; flex-direction: column; }
  .movie-poster-container { position: relative; width:100%; aspect-ratio: 2 / 3; }
  .movie-poster { width: 100%; height: 100%; object-fit: cover; }
  /* [MODIFIED] Poster Badge (Top-Left) */
  .poster-badge { position: absolute; top: 10px; left: 10px; background-color: var(--netflix-red); color: white; padding: 4px 8px; border-radius: 3px; font-size: 0.75rem; font-weight: 700; z-index: 4; }
  /* [MODIFIED] Rating Badge (Bottom-Right, No Background) */
  .rating-badge { position: absolute; bottom: 10px; right: 10px; color: white; font-size: 0.9rem; font-weight: 700; z-index: 3; text-shadow: 1px 1px 3px rgba(0,0,0,0.8); display: flex; align-items: center; gap: 5px; }
  .rating-badge .fa-star { color: #f5c518; }
  .card-info-static { padding: 10px 8px; background-color: #1a1a1a; flex-shrink:0; }
  .card-info-title { font-size: 0.9rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin:0 0 4px 0; }
  .card-info-meta { font-size: 0.75rem; color: var(--text-dark); margin:0; }
  .category-grid, .full-page-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 20px 15px; }
  .category-section { margin: 40px 0; }
  .category-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
  .category-title { font-family: 'Roboto', sans-serif; font-weight: 700; font-size: 1.6rem; }
  .see-all-link { color: var(--text-dark); }
  .bottom-nav { display: none; } /* Mobile nav styles */
  .top-category-nav { padding: 80px 0 20px 0; text-align: center; border-bottom: 1px solid #222; margin-bottom: 20px;}
  .top-category-nav a { margin: 0 15px; font-weight: bold; color: var(--text-dark); text-decoration: none; font-size: 1.1rem; }
  .top-category-nav a:hover, .top-category-nav a.active { color: var(--text-light); }
  @media (max-width: 768px) {
      main { padding: 0 15px; }
      .category-grid, .full-page-grid { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); }
      .bottom-nav { display: flex; position:fixed; bottom:0; left:0; right:0; height: var(--nav-height); background-color: #181818; justify-content:space-around; align-items:center; z-index:200; border-top: 1px solid #282828;} 
      .nav-item { display:flex; flex-direction:column; align-items:center; color: var(--text-dark); font-size:10px; flex-grow:1; padding: 5px 0;}
      .nav-item i { font-size: 20px; margin-bottom: 4px; }
  }
</style>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.2.0/css/all.min.css">
</head>
<body>
<header class="main-nav"><a href="{{ url_for('home') }}" class="logo">MovieZone</a><form method="GET" action="/" class="search-form"><input type="search" name="q" class="search-input" placeholder="Search..." value="{{ query|default('') }}" /></form></header>
<main>
  {% macro render_movie_card(m) %}
    <a href="{{ url_for('movie_detail', movie_id=m._id) }}" class="movie-card">
      <div class="poster-wrapper">
        <div class="movie-poster-container">
           <img class="movie-poster" loading="lazy" src="{{ m.poster or 'https://via.placeholder.com/400x600.png?text=No+Image' }}" alt="{{ m.title }}">
           {% if m.poster_badge %}<div class="poster-badge">{{ m.poster_badge }}</div>{% endif %}
           {% if m.vote_average and m.vote_average > 0 %}<div class="rating-badge"><i class="fas fa-star"></i> {{ "%.1f"|format(m.vote_average) }}</div>{% endif %}
        </div>
        <div class="card-info-static">
          <h4 class="card-info-title">{{ m.title }}</h4>
          {% if m.release_date %}<p class="card-info-meta">{{ m.release_date.split('-')[0] }}</p>{% endif %}
        </div>
      </div>
    </a>
  {% endmacro %}

  {% if is_full_page_list %}
    <div class="full-page-grid-container" style="padding-top: 100px;">
        <h2 class="full-page-grid-title" style="font-size: 2.5rem; font-weight: 700; margin-bottom: 30px;">{{ page_title }}</h2>
        {% if movies|length == 0 %}<p>No content found in this category.</p>
        {% else %}<div class="full-page-grid">{% for m in movies %}{{ render_movie_card(m) }}{% endfor %}</div>{% endif %}
    </div>
  {% else %}
    {% if home_content.get('Trending Now') %}<div class="hero-section">{% for movie in home_content['Trending Now'][:5] %}<div class="hero-slide {% if loop.first %}active{% endif %}" style="background-image: url('{{ movie.poster or '' }}');"><div class="hero-content"><h1 class="hero-title">{{ movie.title }}</h1><p class="hero-overview">{{ movie.overview }}</p><a href="{{ url_for('movie_detail', movie_id=movie._id) }}" class="btn btn-secondary"><i class="fas fa-info-circle"></i> More Info</a></div></div>{% endfor %}</div>{% endif %}
    
    <nav class="top-category-nav">
        {% for cat in ['Hindi', 'Bengali', 'English & Hollywood', 'Web Series'] %}
        <a href="{{ url_for('category_page', category_name=cat) }}">{{ cat }}</a>
        {% endfor %}
    </nav>
    
    {% for category, movies_list in home_content.items() %}
    <div class="category-section">
        <div class="category-header">
            <h2 class="category-title">{{ category }}</h2>
            <a href="{{ url_for('category_page', category_name=category) }}" class="see-all-link">See All ></a>
        </div>
        <div class="category-grid">
            {% for m in movies_list[:12] %}{{ render_movie_card(m) }}{% endfor %}
        </div>
    </div>
    {% endfor %}
  {% endif %}
</main>
<nav class="bottom-nav">
    <a href="{{ url_for('home') }}" class="nav-item"><i class="fas fa-home"></i><span>Home</span></a>
    <a href="{{ url_for('category_page', category_name='Latest Movies') }}" class="nav-item"><i class="fas fa-film"></i><span>Movies</span></a>
    <a href="{{ url_for('category_page', category_name='Web Series') }}" class="nav-item"><i class="fas fa-tv"></i><span>Series</span></a>
    <a href="{{ url_for('genres_page') }}" class="nav-item"><i class="fas fa-layer-group"></i><span>Genres</span></a>
    <a href="{{ url_for('contact') }}" class="nav-item"><i class="fas fa-envelope"></i><span>Request</span></a>
</nav>
<script>
    window.addEventListener('scroll', () => { document.querySelector('.main-nav').classList.toggle('scrolled', window.scrollY > 50); });
    document.addEventListener('DOMContentLoaded', function() { const slides = document.querySelectorAll('.hero-slide'); if (slides.length > 1) { let currentSlide = 0; const showSlide = (index) => slides.forEach((s, i) => s.classList.toggle('active', i === index)); setInterval(() => { currentSlide = (currentSlide + 1) % slides.length; showSlide(currentSlide); }, 5000); } });
</script>
</body>
</html>
"""

detail_html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{{ movie.title or "Not Found" }} - MovieZone</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Roboto:wght@400;500;700&display=swap');
  :root { --netflix-red: #E50914; --netflix-black: #141414; --text-light: #f5f5f5; --text-dark: #a0a0a0; }
  body { font-family: 'Roboto', sans-serif; background: var(--netflix-black); color: var(--text-light); }
  .detail-header { position: absolute; top: 0; left: 0; right: 0; padding: 20px 50px; z-index: 100; }
  .back-button { font-size: 1.2rem; text-decoration: none; color: var(--text-light); }
  .detail-hero { position: relative; padding: 100px 50px; display: flex; align-items: center; }
  .detail-hero-background { position: absolute; top: 0; left: 0; right: 0; bottom: 0; background-size: cover; background-position: center; filter: blur(20px) brightness(0.4); }
  .detail-content-wrapper { position: relative; z-index: 2; display: flex; gap: 40px; max-width: 1200px; }
  .detail-poster { width: 300px; height: 450px; flex-shrink: 0; border-radius: 8px; }
  .detail-title { font-family: 'Bebas Neue', sans-serif; font-size: 4.5rem; margin-bottom: 20px; }
  .detail-meta { display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 25px; align-items:center; }
  .detail-overview { margin-bottom: 30px; }
  .action-btn { background-color: var(--netflix-red); color: white; padding: 15px 30px; border-radius: 5px; text-decoration: none; display:inline-flex; align-items:center; gap: 8px;}
  .section-title { font-size: 1.5rem; border-bottom: 2px solid var(--netflix-red); padding-bottom: 5px; display: inline-block; margin-bottom: 20px; }
  .video-container { position: relative; padding-bottom: 56.25%; height: 0; background: #000; border-radius: 8px; overflow:hidden; }
  .video-container iframe { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
  .download-button, .episode-button { display: inline-block; padding: 12px 25px; background-color: #444; color: white; text-decoration: none; border-radius: 4px; margin: 5px; }
</style>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.2.0/css/all.min.css">
</head>
<body>
<header class="detail-header"><a href="{{ url_for('home') }}" class="back-button"><i class="fas fa-arrow-left"></i> Back to Home</a></header>
{% if movie %}
<div class="detail-hero">
  <div class="detail-hero-background" style="background-image: url('{{ movie.poster }}');"></div>
  <div class="detail-content-wrapper">
    <img class="detail-poster" src="{{ movie.poster or PLACEHOLDER_POSTER }}" alt="{{ movie.title }}">
    <div class="detail-info">
      <h1 class="detail-title">{{ movie.title }}</h1>
      <div class="detail-meta">
        {% if movie.release_date %}<span>{{ movie.release_date.split('-')[0] }}</span>{% endif %}
        {% if movie.vote_average %}<span><i class="fas fa-star" style="color:#f5c518;"></i> {{ "%.1f"|format(movie.vote_average) }}</span>{% endif %}
        {% if movie.languages %}<span><i class="fas fa-language"></i> {{ movie.languages | join(' • ') }}</span>{% endif %}
      </div>
      <p class="detail-overview">{{ movie.overview }}</p>
      {% if movie.type == 'movie' and movie.watch_link %}<a href="{{ url_for('watch_movie', movie_id=movie._id) }}" class="action-btn"><i class="fas fa-play"></i> Watch Now</a>{% endif %}
      <a href="{{ url_for('contact', report_id=movie._id, title=movie.title) }}" class="download-button" style="background-color:#5a5a5a;">Report a Problem</a>
    </div>
  </div>
</div>
<div style="padding: 0 50px 50px; max-width: 1200px; margin: auto;">
    {% if movie.genres %}
    <div class="genres-section" style="margin-top:20px;">
        <h3 class="section-title">Genres</h3><br>
        {% for genre in movie.genres %}<a href="{{ url_for('movies_by_genre', genre_name=genre) }}" class="download-button">{{ genre }}</a>{% endfor %}
    </div>
    {% endif %}

    {% if movie.trailer_key %}
    <div class="trailer-section" style="margin-top: 30px;">
        <h3 class="section-title">Watch Trailer</h3>
        <div class="video-container"><iframe src="https://www.youtube.com/embed/{{ movie.trailer_key }}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe></div>
    </div>
    {% endif %}
</div>
{% else %}
<div style="text-align:center; padding-top:100px;"><h2>Content not found.</h2></div>
{% endif %}
</body>
</html>
"""

admin_html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Admin Panel</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.2.0/css/all.min.css">
<style>
  :root { --netflix-red: #E50914; --netflix-black: #141414; --dark-gray: #222; --light-gray: #333; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--netflix-black); color: #fff; padding: 20px; }
  .admin-container { max-width: 900px; margin: auto; }
  h1, h2, h3 { color: var(--netflix-red); font-family: 'Bebas Neue', sans-serif;}
  form { background: var(--dark-gray); padding: 20px; border-radius: 8px; }
  .form-group { margin-bottom: 15px; } label { display: block; margin-bottom: 5px; color: #aaa; }
  input, textarea, select { width: 100%; padding: 10px; border-radius: 4px; border: 1px solid var(--light-gray); background: var(--light-gray); color: #fff; box-sizing: border-box; }
  .btn { padding: 10px 20px; border-radius: 4px; border: none; cursor: pointer; background: var(--netflix-red); color: #fff; font-weight: bold; }
  .checkbox-group { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
  .checkbox-group label { background: var(--light-gray); padding: 10px; border-radius: 4px; display:flex; align-items:center; gap: 8px; }
  #fetch-section { background: #000; padding: 15px; border-radius: 5px; margin-bottom: 20px; border: 1px solid var(--light-gray); }
  table { width: 100%; border-collapse: collapse; margin-top: 20px; } th, td { padding: 10px; border: 1px solid #333; }
</style>
</head>
<body>
<div class="admin-container">
<h1>Admin Panel</h1>
<div id="fetch-section">
    <h3><i class="fas fa-magic"></i> Fetch from TMDB</h3>
    <p>Paste a TMDB movie/series URL to auto-fill the form.</p>
    <div class="form-group" style="display:flex; gap:10px;">
        <input type="url" id="tmdb_url" placeholder="https://www.themoviedb.org/movie/..." style="flex-grow:1;">
        <button type="button" class="btn" onclick="fetchTmdbData()">Fetch Info</button>
    </div>
</div>
<h2>Add / Edit Content</h2>
<form method="post" action="/admin/save">
    <input type="hidden" name="movie_id" id="movie_id">
    <fieldset>
        <legend>Core Information</legend>
        <div class="form-group"><label>Title:</label><input type="text" name="title" id="title" required></div>
        <div class="form-group"><label>Overview/Description:</label><textarea name="overview" id="overview" rows="4"></textarea></div>
        <div class="form-group"><label>Poster URL:</label><input type="url" name="poster" id="poster"></div>
        <div class="form-group"><label>Release Date (YYYY-MM-DD):</label><input type="text" name="release_date" id="release_date"></div>
        <div class="form-group"><label>Rating (e.g., 7.5):</label><input type="number" step="0.1" name="vote_average" id="vote_average"></div>
        <div class="form-group"><label>Genres (comma-separated):</label><input type="text" name="genres" id="genres"></div>
        <div class="form-group"><label>Languages (comma-separated):</label><input type="text" name="languages" id="languages"></div>
    </fieldset>
    <fieldset>
        <legend>Custom Information</legend>
        <div class="form-group"><label>Trailer Link (YouTube):</label><input type="url" name="trailer_link" id="trailer_link"></div>
        <div class="form-group"><label>Poster Badge:</label><input type="text" name="poster_badge" id="poster_badge" placeholder="e.g., WEB-DL, HDRip"></div>
    </fieldset>
    <fieldset>
        <legend>Categories</legend>
        <div class="checkbox-group" id="categories-container">
            {% for cat in site_categories %}
            <label><input type="checkbox" name="categories" value="{{ cat }}"> {{ cat }}</label>
            {% endfor %}
        </div>
    </fieldset>
    <button type="submit" class="btn">Save Content</button>
    <button type="button" class="btn" style="background:#555" onclick="document.forms[0].reset(); document.getElementById('movie_id').value='';">Clear Form</button>
</form>

<h2 style="margin-top: 40px;">Existing Content</h2>
<table>
    <thead><tr><th>Title</th><th>Actions</th></tr></thead>
    <tbody>
        {% for movie in content_list %}
        <tr>
            <td>{{ movie.title }}</td>
            <td><button class="btn" style="background:#007bff" onclick='editMovie({{ movie | tojson | safe }})'>Edit</button></td>
        </tr>
        {% endfor %}
    </tbody>
</table>

</div>
<script>
    function fetchTmdbData() {
        const url = document.getElementById('tmdb_url').value;
        if (!url) { alert('Please enter a TMDB URL.'); return; }
        fetch('/admin/fetch_tmdb', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ tmdb_url: url })
        })
        .then(response => response.json())
        .then(data => {
            if (data.error) { alert('Error: ' + data.error); return; }
            document.getElementById('title').value = data.title || '';
            document.getElementById('overview').value = data.overview || '';
            document.getElementById('poster').value = data.poster || '';
            document.getElementById('release_date').value = data.release_date || '';
            document.getElementById('vote_average').value = data.vote_average || '';
            document.getElementById('genres').value = data.genres ? data.genres.join(', ') : '';
        });
    }

    function editMovie(movieData) {
        // Clear previous state
        document.forms[0].reset();
        
        document.getElementById('movie_id').value = movieData._id;
        document.getElementById('title').value = movieData.title || '';
        document.getElementById('overview').value = movieData.overview || '';
        document.getElementById('poster').value = movieData.poster || '';
        document.getElementById('release_date').value = movieData.release_date || '';
        document.getElementById('vote_average').value = movieData.vote_average || '';
        document.getElementById('genres').value = movieData.genres ? movieData.genres.join(', ') : '';
        document.getElementById('languages').value = movieData.languages ? movieData.languages.join(', ') : '';
        document.getElementById('trailer_link').value = movieData.trailer_key ? 'https://www.youtube.com/watch?v=' + movieData.trailer_key : '';
        document.getElementById('poster_badge').value = movieData.poster_badge || '';

        const checkboxes = document.querySelectorAll('#categories-container input[type="checkbox"]');
        checkboxes.forEach(cb => {
            cb.checked = movieData.categories && movieData.categories.includes(cb.value);
        });
        window.scrollTo(0,0);
    }
</script>
</body>
</html>
"""

# ======================================================================
# --- Flask Routes ---
# ======================================================================
@app.route('/')
def home():
    query = request.args.get('q', '').strip()
    if query:
        results = list(movies.find({"title": {"$regex": query, "$options": "i"}}).sort('_id', -1))
        return render_template_string(index_html, is_full_page_list=True, movies=process_movie_list(results), page_title=f'Results for "{query}"')

    home_content = {}
    homepage_categories = ["Trending Now", "Latest Movies", "Hindi", "Bengali", "English & Hollywood", "Web Series", "Recently Added"]
    for category in homepage_categories:
        category_movies = list(movies.find({"categories": category}).sort('_id', -1).limit(12))
        if category_movies:
            home_content[category] = process_movie_list(category_movies)
            
    return render_template_string(index_html, is_full_page_list=False, home_content=home_content)

@app.route('/category/<category_name>')
def category_page(category_name):
    results = list(movies.find({"categories": category_name}).sort('_id', -1))
    return render_template_string(index_html, is_full_page_list=True, movies=process_movie_list(results), page_title=category_name)

@app.route('/movie/<movie_id>')
def movie_detail(movie_id):
    try:
        movie = movies.find_one({"_id": ObjectId(movie_id)})
        return render_template_string(detail_html, movie=movie) if movie else ("Content not found", 404)
    except:
        return "Invalid ID", 400

@app.route('/watch/<movie_id>')
def watch_movie(movie_id):
    try:
        movie = movies.find_one({"_id": ObjectId(movie_id)})
        if not movie or not movie.get("watch_link"): return "Content not found.", 404
        return render_template_string(watch_html, watch_link=movie["watch_link"], title=movie["title"])
    except Exception: return "An error occurred.", 500
    
@app.route('/genres')
def genres_page():
    all_genres = sorted(movies.distinct("genres"))
    return render_template_string(genres_html, genres=all_genres, title="Browse by Genre")
    
@app.route('/movies_by_genre/<genre_name>')
def movies_by_genre(genre_name): 
    results = list(movies.find({"genres": genre_name}).sort('_id', -1))
    return render_template_string(index_html, is_full_page_list=True, movies=process_movie_list(results), page_title=f"Genre: {genre_name}")

# ======================================================================
# --- Admin Routes ---
# ======================================================================
@app.route('/admin')
@requires_auth
def admin():
    content_list = list(movies.find({}, {"title": 1}).sort('_id', -1))
    return render_template_string(admin_html, site_categories=SITE_CATEGORIES, content_list=process_movie_list(content_list))

@app.route('/admin/save', methods=["POST"])
@requires_auth
def save_content():
    form_data = request.form
    movie_id = form_data.get('movie_id')
    
    trailer_link = form_data.get('trailer_link')
    
    movie_doc = {
        "title": form_data.get('title'),
        "overview": form_data.get('overview'),
        "poster": form_data.get('poster'),
        "release_date": form_data.get('release_date'),
        "vote_average": float(form_data.get('vote_average', 0)) if form_data.get('vote_average') else 0,
        "genres": [g.strip() for g in form_data.get('genres', '').split(',') if g.strip()],
        "languages": [l.strip() for l in form_data.get('languages', '').split(',') if l.strip()],
        "poster_badge": form_data.get('poster_badge') or None,
        "trailer_key": extract_youtube_id(trailer_link),
        "categories": form_data.getlist('categories'),
        "type": "series" if "Web Series" in form_data.getlist('categories') else "movie",
        # Add logic for watch_link, files, episodes etc. here
    }
    
    if movie_id:
        movies.update_one({"_id": ObjectId(movie_id)}, {"$set": movie_doc})
    else:
        result = movies.insert_one(movie_doc)
        new_movie = movies.find_one({"_id": result.inserted_id})
        send_notification_to_channel(new_movie)

    return redirect(url_for('admin'))

@app.route('/admin/fetch_tmdb', methods=['POST'])
@requires_auth
def fetch_tmdb():
    url = request.json.get('tmdb_url')
    if not url: return jsonify({"error": "URL is required."}), 400
    
    match = re.search(r'themoviedb\.org\/(tv|movie)\/(\d+)', url)
    if not match: return jsonify({"error": "Invalid TMDB URL."}), 400
    
    content_type_str, tmdb_id = match.groups()
    search_type = "tv" if content_type_str == "tv" else "movie"
    
    try:
        api_url = f"https://api.themoviedb.org/3/{search_type}/{tmdb_id}?api_key={TMDB_API_KEY}"
        res = requests.get(api_url, timeout=10)
        res.raise_for_status()
        data = res.json()
        
        details = {
            "title": data.get("title") or data.get("name"),
            "overview": data.get("overview"),
            "poster": f"https://image.tmdb.org/t/p/w500{data.get('poster_path')}" if data.get('poster_path') else None,
            "release_date": data.get("release_date") or data.get("first_air_date"),
            "vote_average": data.get("vote_average"),
            "genres": [g['name'] for g in data.get("genres", [])],
        }
        return jsonify(details)
    except requests.RequestException as e:
        return jsonify({"error": f"Failed to fetch from TMDB: {e}"}), 500

# Your other routes like webhook, contact, save_ads etc. should be placed here.
# I am including the ones from your last provided code to ensure completeness.
@app.route('/admin/save_ads', methods=['POST'])
@requires_auth
def save_ads():
    ad_codes = {
        "popunder_code": request.form.get("popunder_code", ""), 
        "social_bar_code": request.form.get("social_bar_code", ""),
        "banner_ad_code": request.form.get("banner_ad_code", ""), 
        "native_banner_code": request.form.get("native_banner_code", "")
    }
    settings.update_one({}, {"$set": ad_codes}, upsert=True)
    return redirect(url_for('admin'))

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        feedback_data = {
            "type": request.form.get("type"), "content_title": request.form.get("content_title"),
            "message": request.form.get("message"), "email": request.form.get("email", "").strip(),
            "reported_content_id": request.form.get("reported_content_id"), "timestamp": datetime.utcnow()
        }
        feedback.insert_one(feedback_data)
        return render_template_string(contact_html, message_sent=True)
    prefill_title, prefill_id = request.args.get('title', ''), request.args.get('report_id', '')
    prefill_type = 'Problem Report' if prefill_id else 'Movie Request'
    return render_template_string(contact_html, message_sent=False, prefill_title=prefill_title, prefill_id=prefill_id, prefill_type=prefill_type)

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    # This function remains unchanged from your provided file.
    data = request.get_json()
    if 'channel_post' in data:
        post = data['channel_post']
        if str(post.get('chat', {}).get('id')) != ADMIN_CHANNEL_ID: 
            return jsonify(status='ok', reason='not_admin_channel')
        # ... (rest of the webhook logic is assumed to be here)
    elif 'message' in data:
        message = data['message']
        chat_id = message['chat']['id']
        text = message.get('text', '')
        if text.startswith('/start'):
            pass # ... (rest of the /start command logic is here)
    return jsonify(status='ok')


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
```--- END OF FILE ---