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
# [ADDED] Define the categories available in the admin panel
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

# [ADDED] Helper function to extract YouTube ID from any valid URL
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
  .poster-wrapper { position: relative; width: 100%; border-radius: 6px; overflow: hidden; background-color: #222; display: flex; flex-direction: column;}
  .movie-poster-container { position: relative; width:100%; aspect-ratio: 2 / 3; }
  .movie-poster { width: 100%; height: 100%; object-fit: cover; }
  /* [MODIFIED] Poster Badge (Top-Left) */
  .poster-badge { position: absolute; top: 10px; left: 10px; background-color: var(--netflix-red); color: white; padding: 4px 8px; border-radius: 3px; font-size: 0.75rem; font-weight: 700; z-index: 4; }
  /* [MODIFIED] Rating Badge (Bottom-Right, No Background) */
  .rating-badge { position: absolute; bottom: 10px; right: 10px; color: white; background-color: transparent; font-size: 0.9rem; font-weight: 700; z-index: 3; text-shadow: 1px 1px 3px rgba(0,0,0,0.8); display: flex; align-items: center; gap: 5px; }
  .rating-badge .fa-star { color: #f5c518; }
  .card-info-static { padding: 10px 8px; background-color: #1a1a1a; flex-shrink:0; }
  .card-info-title { font-size: 0.9rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin:0; }
  .card-info-meta { font-size: 0.75rem; color: var(--text-dark); margin:0; }
  .category-grid, .full-page-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 20px 15px; }
  .category-section { margin: 40px 0; }
  .category-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
  .category-title { font-family: 'Roboto', sans-serif; font-weight: 700; font-size: 1.6rem; }
  .see-all-link { color: var(--text-dark); }
  .bottom-nav { display: none; } /* Mobile nav styles */
  .top-category-nav { padding: 80px 0 20px 0; text-align: center; border-bottom: 1px solid #222; }
  .top-category-nav a { margin: 0 15px; font-weight: bold; color: var(--text-dark); text-decoration: none; font-size: 1.1rem; }
  .top-category-nav a:hover, .top-category-nav a.active { color: var(--text-light); }
  .full-page-grid-container { padding-top: 100px; }
  @media (max-width: 768px) {
      main { padding: 0 15px; }
      .category-grid, .full-page-grid { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); }
      .bottom-nav { display: flex; position:fixed; bottom:0; left:0; right:0; height: var(--nav-height); background-color: #181818; justify-content:space-around; align-items:center; z-index:200; border-top: 1px solid #282828;} 
      .nav-item { display:flex; flex-direction:column; align-items:center; color: var(--text-dark); font-size:10px; flex-grow:1; padding: 5px 0; }
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
    <div class="full-page-grid-container">
        <h2 class="full-page-grid-title">{{ page_title }}</h2>
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
            {% for m in movies_list %}{{ render_movie_card(m) }}{% endfor %}
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