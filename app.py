import os
import io
from flask import Flask, request, send_file, redirect, url_for, render_template_string, flash, after_this_request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image
import ffmpeg # Nécessite la librairie ffmpeg sur l'environnement d'hébergement (Render/Dockerfile)
import zipfile
import yt_dlp

# --------------------------
# 1. CONFIGURATION
# --------------------------

UPLOAD_FOLDER = 'uploads'
CONVERTED_FOLDER = 'converted'

app = Flask(__name__)

# LECTURE DE LA CLÉ SECRÈTE DEPUIS L'ENVIRONNEMENT
app.secret_key = os.environ.get('SECRET_KEY', 'CLE_SECRETE_TRES_LONGUE_ET_UNIQUE_DE_SECOURS')

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['CONVERTED_FOLDER'] = CONVERTED_FOLDER
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# CORRECTION CRITIQUE POUR RENDER : Utilisation de PostgreSQL via l'environnement
RAW_DATABASE_URL = os.environ.get(
    'DATABASE_URL', 
    'postgresql://user:password@host:port/dbname' # Placeholder
) 
# Fix common error: replace 'postgres://' with 'postgresql://'
if RAW_DATABASE_URL.startswith('postgres://'):
    database_url = RAW_DATABASE_URL.replace('postgres://', 'postgresql://', 1)
else:
    database_url = RAW_DATABASE_URL
app.config['SQLALCHEMY_DATABASE_URI'] = database_url

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Création des dossiers au démarrage
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(CONVERTED_FOLDER, exist_ok=True)

# --------------------------
# 2. MODÈLE DE BASE DE DONNÉES
# --------------------------

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    # Taille augmentée pour le hash de mot de passe (si Flask-Login utilise un algo long)
    password_hash = db.Column(db.String(256), nullable=False) 

    def set_password(self, password):
        # Utilisation de sha256 par défaut pour werkzeug
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --------------------------
# 3. INITIALISATION DB POUR RENDER
# --------------------------
# Ce bloc est crucial pour créer les tables PostgreSQL au démarrage
with app.app_context():
    print("Tentative de CRÉATION des tables PostgreSQL...")
    try:
        db.create_all() 
        print("Tables de la base de données créées/vérifiées avec succès.")
    except Exception as e:
        print(f"Échec de la création des tables lors du démarrage: {e}")


# --------------------------
# 4. FONCTIONS UTILITAIRES
# --------------------------

def allowed_file(filename, extensions):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in extensions

# --------------------------
# 5. TEMPLATE HTML (Non modifié)
# --------------------------

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

# --------------------------
# 6. ROUTES FLASK
# --------------------------

@app.route('/register', methods=['POST'])
def register():
    email = request.form.get('email')
    password = request.form.get('password')
    
    with app.app_context():
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
        
        with app.app_context():
            user = User.query.filter_by(email=email).first()
            
            if not user or not user.check_password(password):
                flash('Email ou mot de passe incorrect.', 'error')
                # Redirige vers la page d'accueil pour réafficher le formulaire
                return redirect(url_for('index'))
            
            login_user(user)
            flash('Connexion réussie.', 'success')
            return redirect(url_for('index'))
    
    # Pour les requêtes GET ou non POST
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
    
    if not url or not output_format:
        flash('Veuillez fournir une URL et un format de sortie.', 'error')
        return redirect(url_for('index'))
    
    # Fichiers temporaires pour yt-dlp/ffmpeg
    temp_title = f"{current_user.id}_dl_temp" 
    temp_download_path_template = os.path.join(app.config['CONVERTED_FOLDER'], f'{temp_title}.%(ext)s')
    downloaded_file = os.path.join(app.config['CONVERTED_FOLDER'], f'{temp_title}.mp4')
    output_path_mp3 = os.path.join(app.config['CONVERTED_FOLDER'], f'{temp_title}.mp3')

    
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best',
        # yt-dlp essaie de nommer le fichier de sortie selon ce template.
        'outtmpl': temp_download_path_template, 
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30, 
    }

    original_title = 'video_download'
    
    # ----------------------------------------------------
    # FONCTION DE NETTOYAGE QUI S'EXÉCUTERA APRÈS LA RÉPONSE
    # ----------------------------------------------------
    @after_this_request
    def cleanup_files(response):
        # Cette fonction s'exécute même si l'opération échoue avant send_file
        files_to_remove = [downloaded_file, output_path_mp3]
        
        try:
            for f in files_to_remove:
                if os.path.exists(f):
                    os.remove(f)
        except Exception as e:
            app.logger.error(f"Erreur de nettoyage de fichier pour {current_user.id}: {e}") 
        return response
    # ----------------------------------------------------

    try:
        # Nettoyage des résidus précédents au cas où
        if os.path.exists(downloaded_file):
            os.remove(downloaded_file)
            
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # 1. Obtention du titre
            info_dict = ydl.extract_info(url, download=False)
            original_title = info_dict.get('title', 'video_download')
            # Nettoyage des caractères illégaux/sauts de ligne
            original_title = original_title.replace(' ', '_').replace('/', '_').replace('\\', '_').replace('\n', '').replace('\r', '')
            
            # 2. Téléchargement et fusion
            ydl.download([url])

            if not os.path.exists(downloaded_file):
                flash('Erreur: Fichier téléchargé introuvable après l\'opération. Vérifiez l\'URL.', 'error')
                return redirect(url_for('index'))

            if output_format == 'mp3':
                output_filename = original_title + '.mp3'
                final_path = output_path_mp3
                
                try:
                    (
                        ffmpeg
                        .input(downloaded_file)
                        .output(final_path, format='mp3', acodec='libmp3lame')
                        .run(overwrite_output=True, quiet=True)
                    )
                except ffmpeg.Error as e:
                    flash(f"Erreur de conversion MP3: L'outil FFmpeg est peut-être mal configuré. Détails: {e.stderr.decode('utf8')[:200]}...", 'error')
                    return redirect(url_for('index'))
                
            elif output_format == 'mp4':
                output_filename = original_title + '.mp4'
                final_path = downloaded_file
                
            else:
                flash('Format de sortie non pris en charge.', 'error')
                return redirect(url_for('index'))

            # Renvoyer le fichier
            response = send_file(final_path, as_attachment=True, download_name=output_filename)
            
            # La fonction cleanup_files s'exécutera APRÈS l'envoi du fichier
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
        flash('Aucun fichier n\'a été envoyé', 'error')
        return redirect(url_for('index'))
    
    file = request.files['file']
    
    if file.filename == '':
        flash('Aucun fichier sélectionné', 'error')
        return redirect(url_for('index'))
        
    if file and allowed_file(file.filename, {'gif'}):
        filename = secure_filename(file.filename)
        upload_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(upload_path)
        
        base_name = filename.rsplit('.', 1)[0]
        # Utilisation du user_id pour éviter les collisions
        temp_dir = os.path.join(app.config['CONVERTED_FOLDER'], f"{current_user.id}_{base_name}_frames_temp")
        os.makedirs(temp_dir, exist_ok=True) 

        zip_filename = base_name + '_frames.zip'
        zip_path = os.path.join(app.config['CONVERTED_FOLDER'], zip_filename)

        try:
            img = Image.open(upload_path)
            
            for frame_index in range(img.n_frames):
                img.seek(frame_index)
                frame_filename = f"{base_name}_{frame_index:03d}.png"
                frame_path = os.path.join(temp_dir, frame_filename)
                
                # Sauvegarde en mémoire puis écriture pour robustesse
                # Utilisation d'un buffer pour la mémoire avant écriture du fichier
                img.save(frame_path, format="PNG")
            
            img.close()

            # Création du fichier ZIP
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for frame_file in os.listdir(temp_dir):
                    full_path = os.path.join(temp_dir, frame_file)
                    # Ajoute le fichier au ZIP avec seulement son nom
                    zipf.write(full_path, os.path.basename(full_path))
            
            # ----------------------------------------------------
            # NETTOYAGE après l'envoi du fichier ZIP
            @after_this_request
            def cleanup_gif_files(response):
                try:
                    # Supprimer le dossier temporaire et son contenu
                    for root, _, files in os.walk(temp_dir, topdown=False):
                        for name in files:
                            os.remove(os.path.join(root, name))
                        os.rmdir(temp_dir)
                    # Supprimer le fichier ZIP créé
                    if os.path.exists(zip_path):
                        os.remove(zip_path)
                    # Supprimer le fichier GIF uploadé
                    if os.path.exists(upload_path):
                        os.remove(upload_path)
                except Exception as e:
                    app.logger.error(f"Erreur de nettoyage GIF: {e}") 
                return response
            # ----------------------------------------------------

            return send_file(zip_path, as_attachment=True, download_name=zip_filename)
            
        except Exception as e:
            flash(f"Erreur lors de la conversion du GIF : {e}", 'error')
            return redirect(url_for('index'))
            
    else:
        flash('Format de fichier non autorisé (doit être .gif)', 'error')
        return redirect(url_for('index'))

@app.route('/convert/mp4', methods=['POST'])
@login_required
def convert_mp4():
    if 'file' not in request.files:
        flash('Aucun fichier n\'a été envoyé', 'error')
        return redirect(url_for('index'))
    
    file = request.files['file']
    
    if file.filename == '':
        flash('Aucun fichier sélectionné', 'error')
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
                .run(overwrite_output=True, quiet=True)
            )
            
            # ----------------------------------------------------
            # NETTOYAGE après l'envoi du fichier MP3
            @after_this_request
            def cleanup_mp4_files(response):
                try:
                    if os.path.exists(upload_path):
                        os.remove(upload_path)
                    if os.path.exists(converted_path):
                        os.remove(converted_path)
                except Exception as e:
                    app.logger.error(f"Erreur de nettoyage MP4: {e}") 
                return response
            # ----------------------------------------------------

            return send_file(converted_path, as_attachment=True, download_name=converted_filename)
            
        except ffmpeg.Error as e:
            error_message = f"Erreur FFmpeg. L'outil est introuvable ou mal configuré. Détails: {e.stderr.decode('utf8')[:200]}..."
            flash(error_message, 'error')
            # Suppression du fichier uploadé immédiatement si conversion échoue
            if os.path.exists(upload_path):
                os.remove(upload_path)
            return redirect(url_for('index'))
        except Exception as e:
            flash(f"Erreur lors de la conversion : {e}", 'error')
            if os.path.exists(upload_path):
                os.remove(upload_path)
            return redirect(url_for('index'))
            
    else:
        flash('Format de fichier non autorisé (doit être .mp4)', 'error')
        return redirect(url_for('index'))

# --------------------------
# 7. LANCEMENT
# --------------------------

if __name__ == '__main__':
    # Le bloc db.create_all() s'exécute déjà au chargement du module
    app.run(debug=True, host='0.0.0.0')