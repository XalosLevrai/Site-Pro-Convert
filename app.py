import os
import io
import re
import math
from datetime import timedelta # Nécessaire pour définir la durée de la session
from flask import Flask, request, send_file, redirect, url_for, render_template_string, flash, after_this_request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image
import ffmpeg
import zipfile
import yt_dlp

UPLOAD_FOLDER = 'uploads'
CONVERTED_FOLDER = 'converted'
DB_NAME = 'users.db'

app = Flask(__name__)
app.secret_key = 'CLE_SECRETE_TRES_LONGUE_ET_UNIQUE'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['CONVERTED_FOLDER'] = CONVERTED_FOLDER
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_NAME}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- AJOUT CRUCIAL POUR LA PERSISTANCE DE LA CONNEXION ---
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=7) # Connexion conservée pendant 7 jours
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
# ---------------------------------------------------------

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(CONVERTED_FOLDER, exist_ok=True)

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

@login_manager.user_loader
def load_user(user_id):
    # La méthode .get() est l'équivalent moderne de .query.get()
    return db.session.get(User, int(user_id))

def allowed_file(filename, extensions):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in extensions

def create_database(app):
    with app.app_context():
        if not os.path.exists(DB_NAME):
            db.create_all()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Convertisseur Vidéo Pro | Xalos Edition</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; background: linear-gradient(135deg, #e0f2f1 0%, #b2dfdb 100%); color: #333; min-height: 100vh; display: flex; flex-direction: column; }
        .container { max-width: 800px; width: 90%; margin: 50px auto; background: #ffffff; padding: 30px; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.15); flex-grow: 1; }
        h1 { color: #00796b; text-align: center; margin-bottom: 25px; border-bottom: 3px solid #00796b; padding-bottom: 10px; }
        h2 { color: #009688; border-bottom: 1px solid #e0e0e0; padding-bottom: 5px; margin-top: 25px; }

        .auth-forms { display: flex; justify-content: space-around; gap: 20px; margin-bottom: 30px; }
        .auth-section { flex: 1; border: 1px solid #ccc; padding: 20px; border-radius: 8px; background-color: #f9f9f9; }
        input[type="email"], input[type="password"], input[type="text"] { padding: 10px; border: 1px solid #ccc; border-radius: 4px; width: 100%; box-sizing: border-box; margin-bottom: 10px; }
        .converter-section { border: 2px solid #b2dfdb; padding: 20px; margin-bottom: 25px; border-radius: 8px; background-color: #f0fdfc; }

        button { background-color: #009688; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; transition: background-color 0.3s; font-size: 16px; width: 100%; margin-top: 10px; }
        button:hover { background-color: #00796b; }
        select { padding: 10px; border: 1px solid #ccc; border-radius: 4px; width: 100%; box-sizing: border-box; margin-bottom: 10px; }
        
        .message { margin-bottom: 15px; padding: 10px; border-radius: 4px; font-weight: bold; }
        .success { background-color: #e0f2f1; color: #004d40; border: 1px solid #b2dfdb; }
        .error { background-color: #ffcdd2; color: #b71c1c; border: 1px solid #ef9a9a; }
        
        .footer { margin-top: auto; padding: 15px 0; text-align: right; color: #555; font-size: 0.9em; background-color: #e0e0e0; border-top: 1px solid #ccc; }
        .creator-tag { padding-right: 30px; }
    </style>
</head>
<body>

<div class="container">
    <h1>Outil de Conversion Vidéo Professionnel</h1>
    
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
            <div class="message {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}

    {% if not current_user.is_authenticated %}
        <h2>Connexion / Créer un Compte</h2>
        <div class="auth-forms">
            <div class="auth-section">
                <h3>Se connecter</h3>
                <form method="post" action="{{ url_for('login') }}">
                    <input type="email" name="email" placeholder="Email" required>
                    <input type="password" name="password" placeholder="Mot de passe" required>
                    <button type="submit">Connexion Email</button>
                </form>
            </div>
            
            <div class="auth-section">
                <h3>Créer un Compte</h3>
                <form method="post" action="{{ url_for('register') }}">
                    <input type="email" name="email" placeholder="Email" required>
                    <input type="password" name="password" placeholder="Mot de passe" required>
                    <button type="submit">Créer le Compte</button>
                </form>
            </div>
        </div>
    {% else %}
        <p style="text-align: right;">Connecté en tant que: <b>{{ current_user.email }}</b> | <a href="{{ url_for('logout') }}">Déconnexion</a></p>
        
        <h2>Téléchargement & Conversion Vidéo</h2>

        <div class="converter-section">
            <h2>🔗 URL (YouTube/TikTok) en MP4/MP3</h2>
            <form method="post" action="{{ url_for('download_url') }}">
                <label for="video_url">Collez l'URL YouTube ou TikTok :</label>
                <input type="text" id="video_url" name="url" placeholder="Ex: https://www.youtube.com/watch?v=..." required>
                
                <label for="resolution_select">Résolution maximale :</label>
                <select name="resolution" id="resolution_select" required>
                    <option value="1080">1080p (Full HD)</option>
                    <option value="720">720p (HD)</option>
                    <option value="1440">1440p (2K)</option>
                    <option value="best">Meilleure Qualité Disponible</option>
                </select>

                <label for="format_select">Choisir le format de sortie :</label>
                <select name="format" id="format_select" required>
                    <option value="mp4">Vidéo (MP4)</option>
                    <option value="mp3">Audio (MP3)</option>
                </select>

                <button type="submit">Télécharger & Convertir</button>
            </form>
        </div>

        <div class="converter-section">
            <h2>🖼️ Fichier GIF en Trames PNG (Fichier ZIP)</h2>
            <form method="post" action="{{ url_for('convert_gif') }}" enctype="multipart/form-data">
                <label for="gifFile">Sélectionner un fichier GIF :</label>
                <input type="file" id="gifFile" name="file" accept=".gif" required>
                <button type="submit">Convertir en Trames ZIP</button>
            </form>
        </div>

        <div class="converter-section">
            <h2>🎧 Fichier MP4 en MP3 (Audio)</h2>
            <form method="post" action="{{ url_for('convert_mp4') }}" enctype="multipart/form-data">
                <label for="mp4File">Sélectionner un fichier MP4 :</label>
                <input type="file" id="mp4File" name="file" accept=".mp4" required>
                <button type="submit">Convertir en MP3</button>
            </form>
        </div>
        
    {% endif %}
</div>

<footer class="footer">
    <p class="creator-tag">Créateur : Xalos</p>
</footer>

</body>
</html>
"""

@app.route('/register', methods=['POST'])
def register():
    email = request.form.get('email')
    password = request.form.get('password')
    
    user = User.query.filter_by(email=email).first()
    
    if user:
        flash('Cet email est déjà utilisé.', 'error')
        return redirect(url_for('index'))
        
    if not email or not password:
        flash('Email et mot de passe sont requis.', 'error')
        return redirect(url_for('index'))

    new_user = User(email=email)
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.commit()
    
    flash('Compte créé avec succès ! Veuillez vous connecter.', 'success')
    return redirect(url_for('index'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        user = User.query.filter_by(email=email).first()
        
        if not user or not user.check_password(password):
            flash('Email ou mot de passe incorrect.', 'error')
            return redirect(url_for('login'))
        
        # --- MODIFICATION CRUCIALE : Ajout de remember=True ---
        login_user(user, remember=True)
        # ----------------------------------------------------
        
        flash('Connexion réussie. Vous resterez connecté pendant 7 jours.', 'success')
        return redirect(url_for('index'))
    
    return redirect(url_for('index'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Vous avez été déconnecté.', 'success')
    return redirect(url_for('index'))

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/download/url', methods=['POST'])
@login_required
def download_url():
    url = request.form.get('url')
    output_format = request.form.get('format')
    resolution = request.form.get('resolution') 
    
    if not url or not output_format or not resolution:
        flash('Veuillez fournir une URL, un format de sortie et une résolution.', 'error')
        return redirect(url_for('index'))
    
    temp_title = "yt_dlp_temp_file" 
    temp_download_path_template = os.path.join(app.config['CONVERTED_FOLDER'], f'{temp_title}.%(ext)s')
    downloaded_file = os.path.join(app.config['CONVERTED_FOLDER'], f'{temp_title}.mp4')
    
    # ----------------------------------------------------
    # Construction du format en fonction de la résolution
    # ----------------------------------------------------
    if resolution == 'best':
        video_format = 'bestvideo[ext=mp4]'
    else:
        try:
            height = int(resolution)
            video_format = f'bestvideo[height<={height}][ext=mp4]'
        except ValueError:
            video_format = 'bestvideo[ext=mp4]' # Fallback
            
    ydl_format = f'{video_format}+bestaudio[ext=m4a]/best'
    
    # ----------------------------------------------------
    # Variable pour capturer les statistiques finales
    final_stats = []
    
    def progress_hook(d):
        """Capture les statistiques après la fin du téléchargement/fusion."""
        if d['status'] == 'finished':
            final_stats.append({
                'total_size': d.get('total_bytes', d.get('total_bytes_estimate')),
                'elapsed': d.get('elapsed'),
                'speed': d.get('speed'),
            })

    # ----------------------------------------------------

    ydl_opts = {
        'format': ydl_format,
        'outtmpl': temp_download_path_template,
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30, 
        'progress_hooks': [progress_hook],
        # 'proxy': 'socks5://VOTRE_PROXY_IP:PORT', 
    }

    # ----------------------------------------------------
    # FONCTION DE NETTOYAGE (Correction WinError 32)
    # ----------------------------------------------------
    @after_this_request
    def cleanup_files(response):
        try:
            # Nettoie le fichier MP4/vidéo temporaire
            if os.path.exists(downloaded_file):
                os.remove(downloaded_file)
            
            # Nettoie le fichier MP3 s'il a été créé (en recherchant par extension)
            for file in os.listdir(app.config['CONVERTED_FOLDER']):
                if file.endswith('.mp3'):
                    os.remove(os.path.join(app.config['CONVERTED_FOLDER'], file))

        except Exception as e:
            app.logger.error(f"Erreur de nettoyage de fichier: {e}") 
        return response
    # ----------------------------------------------------

    try:
        if os.path.exists(downloaded_file):
             os.remove(downloaded_file)
             
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)
            
            original_title = info_dict.get('title', 'video_download')
            # Nettoyage pour un nom humain et sûr
            original_title = original_title.replace('\n', ' ').replace('\r', '')
            original_title = re.sub(r'[\\/:*?"<>|]', '', original_title).strip()
            
            ydl.download([url])

            if not os.path.exists(downloaded_file):
                flash('Erreur: Fichier téléchargé introuvable après l\'opération. Vérifiez l\'URL.', 'error')
                return redirect(url_for('index'))

            # Traitement des statistiques
            stats_message = ""
            if final_stats:
                stats = final_stats[0]
                total_size = stats.get('total_size', 0)
                elapsed = stats.get('elapsed', 0)
                speed = stats.get('speed', 0)

                # Formatage des unités
                size_mb = total_size / (1024 * 1024) if total_size else 0
                speed_mbps = speed / (1024 * 1024) if speed else 0
                
                stats_message = (
                    f"Taille finale: {size_mb:.2f} Mo. "
                    f"Temps total: {elapsed:.1f} s. "
                    f"Vitesse moyenne: {speed_mbps:.2f} Mo/s."
                )

            # Conversion en MP3 ou renvoi du MP4
            if output_format == 'mp3':
                download_name = original_title + '.mp3'
                output_path = os.path.join(app.config['CONVERTED_FOLDER'], download_name)
                
                try:
                    (
                        ffmpeg
                        .input(downloaded_file)
                        .output(output_path, format='mp3', acodec='libmp3lame')
                        .run(overwrite_output=True, quiet=True)
                    )
                    final_path = output_path
                    flash(f"Conversion MP3 et Téléchargement réussi ! {stats_message}", 'success')
                except ffmpeg.Error as e:
                    flash(f"Erreur de conversion MP3: {e.stderr.decode('utf8')}. Avez-vous configuré FFmpeg?", 'error')
                    return redirect(url_for('index'))
                
            elif output_format == 'mp4':
                download_name = original_title + '.mp4'
                final_path = downloaded_file
                flash(f"Téléchargement MP4 réussi ! {stats_message}", 'success')
                
            else:
                flash('Format de sortie non pris en charge.', 'error')
                return redirect(url_for('index'))

            # Renvoyer le fichier
            response = send_file(final_path, as_attachment=True, download_name=download_name)
            
            return response
            
    except yt_dlp.utils.DownloadError as e:
        flash(f"Erreur de téléchargement: L'URL est invalide ou la vidéo est restreinte. Détails: {e}", 'error')
        return redirect(url_for('index'))
    except Exception as e:
        flash(f"Une erreur inattendue est survenue: {e}", 'error')
        return redirect(url_for('index'))

@app.route('/convert/gif', methods=['POST'])
@login_required
def convert_gif():
    if 'file' not in request.files:
        flash('Aucun fichier n\'a ete envoye', 'error')
        return redirect(url_for('index'))
    
    file = request.files['file']
    
    if file.filename == '':
        flash('Aucun fichier selectionne', 'error')
        return redirect(url_for('index'))
        
    if file and allowed_file(file.filename, {'gif'}):
        filename = secure_filename(file.filename)
        upload_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(upload_path)
        
        base_name = filename.rsplit('.', 1)[0]
        temp_dir = os.path.join(app.config['CONVERTED_FOLDER'], base_name)
        os.makedirs(temp_dir, exist_ok=True) 

        zip_filename = base_name + '_frames.zip'
        zip_path = os.path.join(app.config['CONVERTED_FOLDER'], zip_filename)

        try:
            img = Image.open(upload_path)
            
            for frame_index in range(img.n_frames):
                img.seek(frame_index)
                frame_filename = f"{base_name}_{frame_index:03d}.png"
                frame_path = os.path.join(temp_dir, frame_filename)
                
                img.save(frame_path, format="PNG")
            
            img.close()

            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for root, _, files in os.walk(temp_dir):
                    for frame_file in files:
                        full_path = os.path.join(root, frame_file)
                        zipf.write(full_path, os.path.basename(full_path))
            
            for root, _, files in os.walk(temp_dir, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
            os.rmdir(temp_dir)
            os.remove(upload_path)
            
            flash('Conversion GIF réussie en trames PNG (ZIP).', 'success')
            return send_file(zip_path, as_attachment=True, download_name=zip_filename)
            
        except Exception as e:
            flash(f"Erreur lors de la conversion du GIF : {e}", 'error')
            return redirect(url_for('index'))
            
    else:
        flash('Format de fichier non autorise (doit etre .gif)', 'error')
        return redirect(url_for('index'))

@app.route('/convert/mp4', methods=['POST'])
@login_required
def convert_mp4():
    if 'file' not in request.files:
        flash('Aucun fichier n\'a ete envoye', 'error')
        return redirect(url_for('index'))
    
    file = request.files['file']
    
    if file.filename == '':
        flash('Aucun fichier selectionne', 'error')
        return redirect(url_for('index'))
        
    if file and allowed_file(file.filename, {'mp4'}):
        filename = secure_filename(file.filename)
        upload_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(upload_path)
        
        try:
            converted_filename = filename.rsplit('.', 1)[0] + '.mp3'
            converted_path = os.path.join(app.config['CONVERTED_FOLDER'], converted_filename)
            
            (
                ffmpeg
                .input(upload_path)
                .output(converted_path, format='mp3', vn=None, acodec='libmp3lame')
                .run(overwrite_output=True)
            )
            
            os.remove(upload_path)

            flash('Conversion MP4 en MP3 réussie.', 'success')
            return send_file(converted_path, as_attachment=True, download_name=converted_filename)
            
        except ffmpeg.Error as e:
            error_message = f"Erreur FFmpeg. L'outil est introuvable. Details: {e.stderr.decode('utf8')}"
            flash(error_message, 'error')
            return redirect(url_for('index'))
        except Exception as e:
            flash(f"Erreur lors de la conversion : {e}", 'error')
            return redirect(url_for('index'))
            
    else:
        flash('Format de fichier non autorise (doit etre .mp4)', 'error')
        return redirect(url_for('index'))

if __name__ == '__main__':
    create_database(app) 
    app.run(debug=True, host='0.0.0.0') # Utilise 0.0.0.0 pour l'accès local