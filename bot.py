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

detail_html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{{ movie.title or "Not Found" }} - PMWBD</title>
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
  .detail-info { flex-grow: 1; }
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
<header class="detail-header"><a href="{{ url_for('home') }}" class="back-button">&larr; Back to Home</a></header>
{% if movie %}
<div class="detail-hero">
  <div class="detail-hero-background" style="background-image: url('{{ movie.poster }}');"></div>
  <div class="detail-content-wrapper">
    <img class="detail-poster" src="{{ movie.poster or 'https://via.placeholder.com/400x600.png?text=No+Image' }}" alt="{{ movie.title }}">
    <div class="detail-info">
      <h1 class="detail-title">{{ movie.title }}</h1>
      <div class="detail-meta">
        {% if movie.release_date %}<span>{{ movie.release_date.split('-')[0] }}</span>{% endif %}
        {% if movie.vote_average %}<span>⭐ {{ "%.1f"|format(movie.vote_average) }}</span>{% endif %}
        {% if movie.languages %}<span>{{ movie.languages | join(' • ') }}</span>{% endif %}
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
        <div class="video-container"><iframe src="https://www.youtube.com/embed/{{ movie.trailer_key }}" frameborder="0" allowfullscreen></iframe></div>
    </div>
    {% endif %}

    <!-- Download & Episode Logic from original file -->
      {% if "Coming Soon" in movie.categories %}<h3 class="section-title">Coming Soon</h3>
      {% elif movie.type == 'movie' %}
        <div class="download-section" style="margin-top: 30px;">
          {% if movie.links %}<h3 class="section-title">Download Links</h3>{% for link_item in movie.links %}<div><a class="download-button" href="{{ link_item.url }}" target="_blank" rel="noopener"><i class="fas fa-download"></i> {{ link_item.quality }}</a></div>{% endfor %}{% endif %}
          {% if movie.files %}<h3 class="section-title">Get from Telegram</h3>{% for file in movie.files | sort(attribute='quality') %}<a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_{{ file.quality }}" class="action-btn" style="background-color: #2AABEE; display:block; text-align:center; margin-top:10px;"><i class="fa-brands fa-telegram"></i> Get {{ file.quality }}</a>{% endfor %}{% endif %}
        </div>
      {% elif movie.type == 'series' %}
        <div class="episode-section" style="margin-top: 30px;">
          <h3 class="section-title">Episodes & Seasons</h3>
          {% if movie.season_packs %}{% for pack in movie.season_packs | sort(attribute='quality') | sort(attribute='season') %}<div class="episode-item"><span class="episode-title">Complete Season {{ pack.season }} Pack ({{ pack.quality }})</span><a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_S{{ pack.season }}_{{ pack.quality }}" class="episode-button"><i class="fas fa-box-open"></i> Get Pack</a></div>{% endfor %}{% endif %}
          {% if movie.episodes %}{% for ep in movie.episodes | sort(attribute='episode_number') | sort(attribute='season') %}<div class="episode-item"><span class="episode-title">S{{ ep.season }} - E{{ ep.episode_number }}</span><a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_{{ ep.season }}_{{ ep.episode_number }}" class="episode-button"><i class="fa-brands fa-telegram"></i> Get Episode</a></div>{% endfor %}{% endif %}
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
  body { font-family: sans-serif; background: var(--netflix-black); color: #fff; padding: 20px; }
  .admin-container { max-width: 900px; margin: auto; }
  h1, h2, h3 { color: var(--netflix-red); font-family: 'Bebas Neue', sans-serif; }
  h1 { font-size: 3rem; } h2 { font-size: 2.2rem; border-left: 4px solid var(--netflix-red); padding-left: 15px; }
  form { background: var(--dark-gray); padding: 20px; border-radius: 8px; }
  fieldset { border: 1px solid var(--light-gray); border-radius: 5px; padding: 20px; margin-bottom: 20px; }
  legend { font-weight: bold; color: var(--netflix-red); padding: 0 10px; font-size: 1.2rem; }
  .form-group { margin-bottom: 15px; } label { display: block; margin-bottom: 5px; color: #aaa; }
  input, textarea, select { width: 100%; padding: 10px; border-radius: 4px; border: 1px solid var(--light-gray); background: var(--light-gray); color: #fff; box-sizing: border-box; }
  .btn { padding: 10px 20px; border-radius: 4px; border: none; cursor: pointer; background: var(--netflix-red); color: #fff; font-weight: bold; text-decoration: none; display: inline-block; }
  .checkbox-group { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
  .checkbox-group label { background: var(--light-gray); padding: 10px; border-radius: 4px; display:flex; align-items:center; gap: 8px; font-size: 0.9rem;}
  #fetch-section { background: #000; padding: 15px; border-radius: 5px; margin-bottom: 20px; border: 1px solid var(--light-gray); }
</style>
</head>
<body>
<div class="admin-container">
<h1>Admin Panel</h1>
<a href="{{ url_for('manage_content') }}" class="btn" style="background-color: #007bff; margin-bottom: 20px;">Manage All Content</a>
<div id="fetch-section">
    <h3><i class="fas fa-magic"></i> Fetch from TMDB</h3>
    <p>Paste a TMDB movie/series URL to auto-fill the form.</p>
    <div class="form-group" style="display:flex; gap:10px;">
        <input type="url" id="tmdb_url" placeholder="https://www.themoviedb.org/movie/..." style="flex-grow:1;">
        <button type="button" class="btn" onclick="fetchTmdbData()">Find</button>
    </div>
</div>
<h2>{{ 'Edit Content' if movie else 'Add New Content' }}</h2>
<form action="{{ url_for('save_content') }}" method="post">
    <input type="hidden" name="movie_id" id="movie_id" value="{{ movie._id if movie else '' }}">
    <fieldset>
        <legend>Core Information</legend>
        <div class="form-group"><label>Title:</label><input type="text" name="title" id="title" value="{{ movie.title if movie else '' }}" required></div>
        <div class="form-group"><label>Overview/Description:</label><textarea name="overview" id="overview" rows="4">{{ movie.overview if movie else '' }}</textarea></div>
        <div class="form-group"><label>Poster URL:</label><input type="url" name="poster" id="poster" value="{{ movie.poster if movie else '' }}"></div>
        <div class="form-group"><label>Release Date (YYYY-MM-DD):</label><input type="text" name="release_date" id="release_date" value="{{ movie.release_date if movie else '' }}"></div>
        <div class="form-group"><label>Rating (e.g., 7.5):</label><input type="number" step="0.1" name="vote_average" id="vote_average" value="{{ movie.vote_average if movie else '' }}"></div>
        <div class="form-group"><label>Genres (comma-separated):</label><input type="text" name="genres" id="genres" value="{{ movie.genres|join(', ') if movie and movie.genres else '' }}"></div>
        <div class="form-group"><label>Languages (comma-separated):</label><input type="text" name="languages" id="languages" value="{{ movie.languages|join(', ') if movie and movie.languages else '' }}"></div>
    </fieldset>
    <fieldset>
        <legend>Custom Information</legend>
        <div class="form-group"><label>Trailer Link (YouTube):</label><input type="url" name="trailer_link" id="trailer_link" value="https://www.youtube.com/watch?v={{ movie.trailer_key if movie and movie.trailer_key else '' }}"></div>
        <div class="form-group"><label>Poster Badge:</label><input type="text" name="poster_badge" id="poster_badge" value="{{ movie.poster_badge if movie else '' }}" placeholder="e.g., WEB-DL"></div>
        <div class="form-group">
            <label>Categories:</label>
            <div class="checkbox-group" id="categories-container">
                {% for cat in site_categories %}
                <label><input type="checkbox" name="categories" value="{{ cat }}" {% if movie and cat in movie.categories %}checked{% endif %}> {{ cat }}</label>
                {% endfor %}
            </div>
        </div>
    </fieldset>
    <fieldset>
        <legend>Links & Files (Manual)</legend>
        <div class="form-group"><label>Watch Link (Embed URL):</label><input type="url" name="watch_link" value="{{ movie.watch_link if movie else '' }}"></div>
        <!-- Add fields for download links and telegram files as needed from your original code -->
    </fieldset>
    <button type="submit" class="btn">Save Content</button>
</form>
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
</script>
</body>
</html>
"""

genres_html = """
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no" /><title>{{ title }} - MovieZone</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Roboto:wght@400;500;700&display=swap');
  :root { --netflix-red: #E50914; --netflix-black: #141414; --text-light: #f5f5f5; }
  * { box-sizing: border-box; margin: 0; padding: 0; } body { font-family: 'Roboto', sans-serif; background-color: var(--netflix-black); color: var(--text-light); } a { text-decoration: none; color: inherit; }
  .main-container { padding: 100px 50px 50px; } .page-title { font-family: 'Bebas Neue', sans-serif; font-size: 3rem; color: var(--netflix-red); margin-bottom: 30px; }
  .back-button { color: var(--text-light); font-size: 1rem; margin-bottom: 20px; display: inline-block; } .back-button:hover { color: var(--netflix-red); }
  .genre-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 20px; }
  .genre-card { background: linear-gradient(45deg, #2c2c2c, #1a1a1a); border-radius: 8px; padding: 30px 20px; text-align: center; font-size: 1.4rem; font-weight: 700; transition: all 0.3s ease; border: 1px solid #444; }
  .genre-card:hover { transform: translateY(-5px) scale(1.03); background: linear-gradient(45deg, var(--netflix-red), #b00710); border-color: var(--netflix-red); }
  @media (max-width: 768px) { .main-container { padding: 80px 15px 30px; } .page-title { font-size: 2.2rem; } .genre-grid { grid-template-columns: repeat(2, 1fr); gap: 15px; } .genre-card { font-size: 1.1rem; padding: 25px 15px; } }
</style><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.2.0/css/all.min.css"></head>
<body>
<div class="main-container"><a href="{{ url_for('home') }}" class="back-button"><i class="fas fa-arrow-left"></i> Back to Home</a><h1 class="page-title">{{ title }}</h1>
<div class="genre-grid">{% for genre in genres %}<a href="{{ url_for('movies_by_genre', genre_name=genre) }}" class="genre-card"><span>{{ genre }}</span></a>{% endfor %}</div></div>
</body></html>
"""

watch_html = """
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Watching: {{ title }}</title>
<style> body, html { margin: 0; padding: 0; height: 100%; overflow: hidden; background-color: #000; } .player-container { width: 100%; height: 100%; } .player-container iframe { width: 100%; height: 100%; border: 0; } </style></head>
<body><div class="player-container"><iframe src="{{ watch_link }}" allowfullscreen allowtransparency allow="autoplay" scrolling="no" frameborder="0"></iframe></div>
</body></html>
"""

contact_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Contact Us / Report - MovieZone</title>
    <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Roboto:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --netflix-red: #E50914; --netflix-black: #141414; --dark-gray: #222; --light-gray: #333; --text-light: #f5f5f5; }
        body { font-family: 'Roboto', sans-serif; background: var(--netflix-black); color: var(--text-light); padding: 20px; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
        .contact-container { max-width: 600px; width: 100%; background: var(--dark-gray); padding: 30px; border-radius: 8px; }
        h2 { font-family: 'Bebas Neue', sans-serif; color: var(--netflix-red); font-size: 2.5rem; text-align: center; margin-bottom: 25px; }
        .form-group { margin-bottom: 20px; } label { display: block; margin-bottom: 8px; font-weight: bold; }
        input, select, textarea { width: 100%; padding: 12px; border-radius: 4px; border: 1px solid var(--light-gray); font-size: 1rem; background: var(--light-gray); color: var(--text-light); box-sizing: border-box; }
        textarea { resize: vertical; min-height: 120px; } button[type="submit"] { background: var(--netflix-red); color: white; font-weight: 700; cursor: pointer; border: none; padding: 12px 25px; border-radius: 4px; font-size: 1.1rem; width: 100%; }
        .success-message { text-align: center; padding: 20px; background-color: #1f4e2c; color: #d4edda; border-radius: 5px; margin-bottom: 20px; }
        .back-link { display: block; text-align: center; margin-top: 20px; color: var(--netflix-red); text-decoration: none; font-weight: bold; }
    </style>
</head>
<body>
<div class="contact-container">
    <h2>Contact Us</h2>
    {% if message_sent %}
        <div class="success-message"><p>Your message has been sent successfully. Thank you!</p></div>
        <a href="{{ url_for('home') }}" class="back-link">← Back to Home</a>
    {% else %}
        <form method="post">
            <div class="form-group"><label for="type">Subject:</label><select name="type" id="type"><option value="Movie Request" {% if prefill_type == 'Problem Report' %}disabled{% endif %}>Movie/Series Request</option><option value="Problem Report" {% if prefill_type == 'Problem Report' %}selected{% endif %}>Report a Problem</option><option value="General Feedback">General Feedback</option></select></div>
            <div class="form-group"><label for="content_title">Movie/Series Title:</label><input type="text" name="content_title" id="content_title" value="{{ prefill_title }}" required></div>
            <div class="form-group"><label for="message">Your Message:</label><textarea name="message" id="message" required></textarea></div>
            <div class="form-group"><label for="email">Your Email (Optional):</label><input type="email" name="email" id="email"></div>
            <input type="hidden" name="reported_content_id" value="{{ prefill_id }}">
            <button type="submit">Submit</button>
        </form>
        <a href="{{ url_for('home') }}" class="back-link">← Cancel</a>
    {% endif %}
</div>
</body>
</html>
"""

# ======================================================================
# --- Main Flask Routes ---
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
        category_movies = list(movies.find({"categories": category, "categories": {"$nin": ["Coming Soon"]}}).sort('_id', -1).limit(12))
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

@app.route('/genre/<genre_name>')
def movies_by_genre(genre_name):
    results = list(movies.find({"genres": genre_name}).sort('_id', -1))
    return render_template_string(index_html, is_full_page_list=True, movies=process_movie_list(results), page_title=f'Genre: {genre_name}')

# ======================================================================
# --- Admin Routes ---
# ======================================================================
@app.route('/admin')
@requires_auth
def admin():
    return render_template_string(admin_html, site_categories=SITE_CATEGORIES, movie=None)

@app.route('/admin/save_content', methods=["POST"])
@requires_auth
def save_content():
    form_data = request.form
    movie_id = form_data.get('movie_id')
    
    # Process numeric fields carefully
    vote_average = form_data.get('vote_average')
    try:
        vote_average = float(vote_average) if vote_average else 0.0
    except (ValueError, TypeError):
        vote_average = 0.0

    movie_doc = {
        "title": form_data.get('title'),
        "overview": form_data.get('overview'),
        "poster": form_data.get('poster'),
        "release_date": form_data.get('release_date'),
        "vote_average": vote_average,
        "genres": [g.strip() for g in form_data.get('genres', '').split(',') if g.strip()],
        "languages": [l.strip() for l in form_data.get('languages', '').split(',') if l.strip()],
        "poster_badge": form_data.get('poster_badge') or None,
        "trailer_key": extract_youtube_id(form_data.get('trailer_link')),
        "categories": form_data.getlist('categories'),
        "type": "series" if "Web Series" in form_data.getlist('categories') else "movie",
        "watch_link": form_data.get("watch_link"),
        "is_coming_soon": "Coming Soon" in form_data.getlist('categories'),
        "is_trending": "Trending Now" in form_data.getlist('categories'),
    }
    
    if movie_id:
        movies.update_one({"_id": ObjectId(movie_id)}, {"$set": movie_doc})
    else:
        movies.insert_one(movie_doc)
        
    return redirect(url_for('manage_content'))

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

@app.route('/admin/manage')
@requires_auth
def manage_content():
    all_movies = list(movies.find().sort('_id', -1))
    return render_template_string("""
        <!DOCTYPE html><html><head><title>Manage Content</title>
        <style>:root { --netflix-red: #E50914; --netflix-black: #141414; --dark-gray: #222; --light-gray: #333; } body{font-family:sans-serif; background:var(--netflix-black); color:#fff;} table{width:100%; border-collapse:collapse;} th,td{border:1px solid #333; padding:8px;} a{color:var(--netflix-red); text-decoration:none;} .btn{padding: 8px 15px; background: var(--netflix-red); color: #fff; border-radius: 4px;}</style>
        </head><body>
        <div style="max-width: 1200px; margin: auto;">
        <h1>Manage Content</h1>
        <a href="{{ url_for('admin') }}" class="btn" style="background-color: #007bff; margin-bottom: 20px;">Add New Content</a>
        <table><thead><tr><th>Title</th><th>Categories</th><th>Actions</th></tr></thead><tbody>
        {% for movie in movies %}
        <tr>
            <td>{{ movie.title }}</td>
            <td>{{ movie.categories|join(', ') if movie.categories else 'N/A' }}</td>
            <td>
                <a href="{{ url_for('edit_movie', movie_id=movie._id) }}">Edit</a> |
                <a href="{{ url_for('delete_movie', movie_id=movie._id) }}" onclick="return confirm('Are you sure?')">Delete</a>
            </td>
        </tr>
        {% endfor %}
        </tbody></table></div></body></html>
    """, movies=process_movie_list(all_movies))

@app.route('/edit_movie/<movie_id>')
@requires_auth
def edit_movie(movie_id):
    movie_data = movies.find_one({"_id": ObjectId(movie_id)})
    return render_template_string(admin_html, site_categories=SITE_CATEGORIES, movie=movie_data)

@app.route('/delete_movie/<movie_id>')
@requires_auth
def delete_movie(movie_id):
    movies.delete_one({"_id": ObjectId(movie_id)})
    return redirect(url_for('manage_content'))
    
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

@app.route('/delete_feedback/<feedback_id>')
@requires_auth
def delete_feedback(feedback_id):
    feedback.delete_one({"_id": ObjectId(feedback_id)})
    return redirect(url_for('admin')) 

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    # This entire function is kept from your original file as it deals with Telegram bot logic
    data = request.get_json()
    if 'channel_post' in data:
        post = data['channel_post']
        if str(post.get('chat', {}).get('id')) != ADMIN_CHANNEL_ID: 
            return jsonify(status='ok', reason='not_admin_channel')
        
        file = post.get('video') or post.get('document')
        if not (file and file.get('file_name')): 
            return jsonify(status='ok', reason='no_file_in_post')
        
        filename = file.get('file_name')
        print(f"\n--- [WEBHOOK] PROCESSING NEW FILE: {filename} ---")
        parsed_info = parse_filename(filename)
        
        if not parsed_info or not parsed_info.get('title'):
            print(f"FAILED: Could not parse title from filename: {filename}")
            return jsonify(status='ok', reason='parsing_failed')
        
        print(f"PARSED INFO: {parsed_info}")

        tmdb_data = get_tmdb_details_from_api(parsed_info['title'], parsed_info['type'], parsed_info.get('year'))

        def get_or_create_content_entry(tmdb_details, parsed_details):
            if tmdb_details and tmdb_details.get("tmdb_id"):
                print(f"INFO: TMDb data found for '{tmdb_details['title']}'. Processing with full details.")
                tmdb_id = tmdb_details.get("tmdb_id")
                existing_entry = movies.find_one({"tmdb_id": tmdb_id})
                if not existing_entry:
                    base_doc = {
                        **tmdb_details, "type": "movie" if parsed_details['type'] == 'movie' else "series",
                        "languages": [], "episodes": [], "season_packs": [], "files": [], "categories": []
                    }
                    movies.insert_one(base_doc)
                    newly_created_doc = movies.find_one({"tmdb_id": tmdb_id})
                    send_notification_to_channel(newly_created_doc)
                    return newly_created_doc
                return existing_entry
            else:
                print(f"WARNING: TMDb data not found for '{parsed_details['title']}'. Using/Creating a placeholder.")
                existing_entry = movies.find_one({
                    "title": {"$regex": f"^{re.escape(parsed_details['title'])}$", "$options": "i"}, 
                    "tmdb_id": None
                })
                if not existing_entry:
                    shell_doc = {
                        "title": parsed_details['title'], "type": "movie" if parsed_details['type'] == 'movie' else "series",
                        "poster": PLACEHOLDER_POSTER, "overview": "Details will be updated soon.",
                        "release_date": None, "genres": [], "vote_average": 0, "trailer_key": None, "tmdb_id": None,
                        "languages": [], "episodes": [], "season_packs": [], "files": [], "categories": []
                    }
                    movies.insert_one(shell_doc)
                    newly_created_doc = movies.find_one({"_id": shell_doc['_id']})
                    send_notification_to_channel(newly_created_doc)
                    return newly_created_doc
                return existing_entry

        content_entry = get_or_create_content_entry(tmdb_data, parsed_info)
        if not content_entry:
            print("FATAL: Could not get or create a content entry.")
            return jsonify(status="error", reason="db_entry_failed")

        update_op = {}
        if parsed_info.get('languages'):
            update_op["$addToSet"] = {"languages": {"$each": parsed_info['languages']}}

        if parsed_info['type'] == 'movie':
            new_file = {"quality": parsed_info.get('quality', 'HD'), "message_id": post['message_id']}
            movies.update_one({"_id": content_entry['_id']}, {"$pull": {"files": {"quality": new_file['quality']}}})
            update_op.setdefault("$push", {})["files"] = new_file
        
        elif parsed_info['type'] == 'series_pack':
            new_pack = {"season": parsed_info['season'], "quality": parsed_info['quality'], "message_id": post['message_id']}
            movies.update_one({"_id": content_entry['_id']}, {"$pull": {"season_packs": {"season": new_pack['season'], "quality": new_pack['quality']}}})
            update_op.setdefault("$push", {})["season_packs"] = new_pack

        elif parsed_info['type'] == 'series':
            new_episode = {"season": parsed_info['season'], "episode_number": parsed_info['episode'], "message_id": post['message_id']}
            movies.update_one({"_id": content_entry['_id']}, {"$pull": {"episodes": {"season": new_episode['season'], "episode_number": new_episode['episode_number']}}})
            update_op.setdefault("$push", {})["episodes"] = new_episode

        if update_op:
            movies.update_one({"_id": content_entry['_id']}, update_op)
            print(f"SUCCESS: Entry for '{content_entry['title']}' has been updated.")

    elif 'message' in data:
        message = data['message']
        chat_id = message['chat']['id']
        text = message.get('text', '')
        if text.startswith('/start'):
            parts = text.split()
            if len(parts) > 1:
                try:
                    payload_parts = parts[1].split('_')
                    doc_id_str = payload_parts[0]
                    content = movies.find_one({"_id": ObjectId(doc_id_str)})
                    if not content: return jsonify(status='ok')

                    message_to_copy_id = None
                    file_info_text = ""
                    
                    if len(payload_parts) == 3 and payload_parts[1].startswith('S'):
                        season_num = int(payload_parts[1][1:])
                        quality = payload_parts[2]
                        pack = next((p for p in content.get('season_packs', []) if p.get('season') == season_num and p.get('quality') == quality), None)
                        if pack:
                            message_to_copy_id = pack.get('message_id')
                            file_info_text = f"Complete Season {season_num} ({quality})"

                    elif content.get('type') == 'series' and len(payload_parts) == 3:
                        s_num, e_num = int(payload_parts[1]), int(payload_parts[2])
                        episode = next((ep for ep in content.get('episodes', []) if ep.get('season') == s_num and ep.get('episode_number') == e_num), None)
                        if episode: 
                            message_to_copy_id = episode.get('message_id')
                            file_info_text = f"S{s_num:02d}E{e_num:02d}"

                    elif content.get('type') == 'movie' and len(payload_parts) == 2:
                        quality = payload_parts[1]
                        file = next((f for f in content.get('files', []) if f.get('quality') == quality), None)
                        if file: 
                            message_to_copy_id = file.get('message_id')
                            file_info_text = f"({quality})"
                    
                    if message_to_copy_id:
                        caption_text = (
                            f"🎬 *{escape_markdown(content['title'])}* {escape_markdown(file_info_text)}\n\n"
                            f"✅ *Successfully Sent To Your PM*\n\n"
                            f"🔰 Join Our Main Channel\n➡️ [{escape_markdown(BOT_USERNAME)} Main]({MAIN_CHANNEL_LINK})\n\n"
                            f"📢 Join Our Update Channel\n➡️ [{escape_markdown(BOT_USERNAME)} Official]({UPDATE_CHANNEL_LINK})\n\n"
                            f"💬 For Any Help or Request\n➡️ [Contact Developer]({DEVELOPER_USER_LINK})"
                        )
                        payload = {'chat_id': chat_id, 'from_chat_id': ADMIN_CHANNEL_ID, 'message_id': message_to_copy_id, 'caption': caption_text, 'parse_mode': 'MarkdownV2'}
                        res = requests.post(f"{TELEGRAM_API_URL}/copyMessage", json=payload).json()
                        
                        if res.get('ok'):
                            new_msg_id = res['result']['message_id']
                            scheduler.add_job(func=delete_message_after_delay, trigger='date', run_date=datetime.now() + timedelta(minutes=30), args=[chat_id, new_msg_id], id=f'del_{chat_id}_{new_msg_id}', replace_existing=True)
                        else: 
                            requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "Error sending file. It might have been deleted from the channel."})
                    else: 
                        requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "Requested file/season not found."})
                except Exception as e:
                    print(f"Error processing /start command: {e}")
                    requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "An unexpected error occurred."})
            else: 
                welcome_message = (f"👋 Welcome to {BOT_USERNAME}!\n\nBrowse all our content on our website.")
                try:
                    with app.app_context():
                        root_url = url_for('home', _external=True)
                    keyboard = {"inline_keyboard": [[{"text": "🎬 Visit Website", "url": root_url}]]}
                    requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': welcome_message, 'reply_markup': json.dumps(keyboard)})
                except Exception as e:
                     print(f"Error sending welcome message: {e}")
                     requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': welcome_message})

    return jsonify(status='ok')

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)```
--- দ্বিতীয় অংশ শেষ ---
