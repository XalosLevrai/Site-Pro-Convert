from flask import Flask, render_template_string, request, redirect, url_for, flash, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import os
import datetime
import random
import secrets 
from PIL import Image 

# --------------------------
# 1. INITIALISATION ET CONFIG
# --------------------------

# IMPORTANT : Le mode asynchrone est crucial pour la compatibilité avec Flask-SocketIO et gunicorn.
# Utiliser 'gevent' ou 'eventlet' (ici, nous partons du principe que vous utiliserez un worker gevent/eventlet avec gunicorn).
app = Flask(__name__)

# LECTURE DE LA CLÉ SECRÈTE DEPUIS L'ENVIRONNEMENT
app.config['SECRET_KEY'] = os.environ.get(
    'SECRET_KEY', 
    'cle_secrete_de_secours_a_ne_pas_utiliser_en_prod' # Changez ceci en production!
)

# VOTRE URL POSTGRES COPIÉE DE RENDER
# ATTENTION: Remplacez ceci par votre véritable URL de base de données Render
RAW_DATABASE_URL = os.environ.get(
    'DATABASE_URL', 
    'postgresql://pro_convert_db_user:votre_mot_de_passe@dpg-d4u4p015pdvs73bnebjg-a.virginia-postgres.render.com/pro_convert_db'
)

# Correction du format de l'URL pour SQLAlchemy (si l'URL commence par 'postgres://')
if RAW_DATABASE_URL.startswith('postgres://'):
    database_url = RAW_DATABASE_URL.replace('postgres://', 'postgresql://', 1)
else:
    database_url = RAW_DATABASE_URL

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False 

# Dossiers d'uploads
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['CONVERTED_FOLDER'] = 'converted'
# CORRECTION DU CARACTÈRE U+00A0 (espace insécable)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024 # Limite d'upload à 100MB

db = SQLAlchemy(app)
# SocketIO initialisé sans app context pour permettre la configuration de gunicorn
socketio = SocketIO(app, cors_allowed_origins="*")

# Créer les dossiers nécessaires s'ils n'existent pas
for folder in [app.config['UPLOAD_FOLDER'], app.config['CONVERTED_FOLDER']]:
    if not os.path.exists(folder):
        os.makedirs(folder)

# Listes temporaires pour le contenu (non persistants après redémarrage)
chat_messages = []
uploaded_videos = [] 
uploaded_images = [] 

# Table pour garder la trace des utilisateurs connectés et de leur ID Socket
user_sid_map = {} 

# --------------------------
# 2. MODÈLES DE BASE DE DONNÉES
# --------------------------

# Table d'association pour la relation plusieurs-à-plusieurs (Amis)
friends = db.Table('friends',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('friend_id', db.Integer, db.ForeignKey('user.id'), primary_key=True)
)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(256), nullable=False) 

    friends = db.relationship(
        'User', 
        secondary=friends,
        primaryjoin=(friends.c.user_id == id),
        secondaryjoin=(friends.c.friend_id == id),
        backref=db.backref('friend_of', lazy='dynamic'),
        lazy='dynamic'
    )
    
    def set_password(self, password):
        self.password = generate_password_hash(password, method='pbkdf2:sha256')

    def check_password(self, password):
        return check_password_hash(self.password, password)

    def add_friend(self, user):
        if not self.is_friend(user):
            self.friends.append(user)
            user.friends.append(self) 

    def is_friend(self, user):
        with app.app_context():
            return self.friends.filter(friends.c.friend_id == user.id).count() > 0

    def __repr__(self):
        return f"User('{self.username}')"

# Création des tables au démarrage (s'assure que le contexte est là pour la DB)
with app.app_context():
    try:
        db.create_all() 
        print("Tables de la base de données créées/vérifiées avec succès.")
    except Exception as e:
        print(f"Échec de la création des tables lors du démarrage: {e}")


# --------------------------
# 3. LE CODE HTML/CSS/JS INTÉGRÉ (MIS À JOUR)
# --------------------------

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>YouTube Python Social</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap');
        body { font-family: 'Roboto', sans-serif; margin: 0; padding: 0; background-color: #181818; color: #FFFFFF; }
        
        /* Header */
        .header { background-color: #202020; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #303030; }
        .logo { font-size: 24px; font-weight: 700; color: #FFFFFF; }
        .logo span { color: #FF0000; margin-left: -4px; } 

        /* Conteneur principal */
        .main-layout { display: flex; max-width: 1600px; margin: 0 auto; }

        /* Sidebar */
        .sidebar { width: 280px; background-color: #282828; padding: 20px 10px; box-sizing: border-box; height: 100vh; position: sticky; top: 0; border-right: 1px solid #303030; overflow-y: auto; }
        .sidebar h3 { color: #AAAAAA; font-size: 14px; margin-top: 20px; padding-bottom: 5px; border-bottom: 1px solid #303030; }
        .sidebar-item { padding: 10px 15px; border-radius: 5px; cursor: pointer; display: flex; align-items: center; font-size: 14px; transition: background-color 0.2s; }
        .sidebar-item:hover { background-color: #383838; }
        .sidebar-item .material-icons { margin-right: 15px; font-size: 20px; color: #909090; }
        .sidebar p strong { color: #00AFFF; font-size: 1em; }


        /* Contenu Principal */
        .content-area { flex-grow: 1; padding: 20px; }
        
        /* Grille de Vidéos */
        .video-grid { 
            display: grid; 
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); 
            gap: 20px; 
            margin-top: 20px; 
        }
        .video-item { color: #FFFFFF; }
        .thumbnail-placeholder { width: 100%; height: 180px; background-color: #303030; display: flex; align-items: center; justify-content: center; border-radius: 8px; margin-bottom: 10px; position: relative; overflow: hidden;}
        .thumbnail-placeholder img { width: 100%; height: 100%; object-fit: cover; }
        .video-details { display: flex; }
        .video-info { margin-left: 10px; }
        .video-info h4 { font-size: 16px; font-weight: 500; margin: 0 0 5px 0; line-height: 1.3; }
        .video-info p { font-size: 12px; color: #AAAAAA; margin: 0; }
        .channel-icon { width: 36px; height: 36px; background: #FF0000; border-radius: 50%; flex-shrink: 0; }
        .video-status-download a { color: #00BFFF; font-weight: 500; text-decoration: none; }
        .video-status-download a:hover { text-decoration: underline; }

        /* Chat Box */
        .chat-container { margin-top: 40px; padding-top: 20px; border-top: 1px solid #303030; }
        .chat-box { height: 300px; border: 1px solid #404040; overflow-y: scroll; padding: 15px; margin-bottom: 15px; background-color: #202020; border-radius: 8px; }
        .message { margin-bottom: 8px; }
        .user-pseudo { font-weight: 500; color: #4CAF50; margin-right: 8px; } 
        .message-input { display: flex; }
        .message-input input { flex-grow: 1; margin-right: 10px; background: #303030; border: 1px solid #404040; color: #FFFFFF; padding: 10px; border-radius: 4px; }
        .message-input button { background-color: #FF0000; color: white; border: none; padding: 10px 15px; border-radius: 4px; cursor: pointer; transition: background-color 0.2s; }
        .message-input button:hover { background-color: #CC0000; }


        /* Formulaires et Boutons d'Action (Sidebar) */
        .auth-form input, .upload-form input, .friend-form input, .util-form input { width: 100%; padding: 10px; margin-bottom: 10px; border: 1px solid #404040; border-radius: 4px; background: #303030; color: #FFFFFF; box-sizing: border-box; }
        .auth-form button, .upload-form button, .friend-form button, .util-form button { width: 100%; padding: 10px; background-color: #FF0000; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: 500; transition: background-color 0.2s; margin-top: 5px;}
        .auth-form button:hover, .upload-form button:hover, .friend-form button:hover, .util-form button:hover { background-color: #CC0000; }
        
        .logout-button { background-color: #555555 !important; }
        .logout-button:hover { background-color: #666666 !important; }

        /* Messages Flash */
        .flash { padding: 15px; margin-bottom: 20px; border-radius: 4px; font-weight: bold; }
        .success { background-color: #4CAF50; color: white; }
        .error { background-color: #FF5555; color: white; }
        .info { background-color: #3498db; color: white; }
        
        .section-title { color: #FFFFFF; font-size: 20px; font-weight: 500; margin-top: 30px; margin-bottom: 15px; }

        /* Liste des images GIF converties */
        .image-grid { display: flex; flex-wrap: wrap; gap: 15px; margin-top: 20px; }
        .image-item { width: 150px; text-align: center; }
        .image-item img { width: 100%; height: 100px; object-fit: cover; border-radius: 4px; border: 1px solid #303030; }
        .image-item a { color: #AAAAAA; text-decoration: none; font-size: 12px; }
        .image-item a:hover { text-decoration: underline; }

    </style>
</head>
<body>
    <div class="header">
        <div class="logo">You<span>Tube</span> (Social Python)</div>
        {% if user_username %}
            <div class="user-action">
                <span style="font-size: 14px; margin-right: 15px; color: #AAAAAA;">Jeton CSRF: {{ csrf_token[:6] }}...</span>
                <span class="material-icons" style="color: white;">account_circle</span>
            </div>
        {% endif %}
    </div>

    <div class="main-layout">
        <div class="sidebar">
            <div class="sidebar-item" onclick="window.location.href='/'">
                <span class="material-icons">home</span> Accueil
            </div>
            
            {% if user_username %}
                <h3>VOTRE COMPTE</h3>
                <p style="padding: 10px 15px; font-size: 14px;">Connecté: <br><strong>@{{ user_username }}</strong></p>
                
                <h3>ACTIONS VIDÉO</h3>
                
                <form class="upload-form" method="POST" action="{{ url_for('upload_file') }}" enctype="multipart/form-data" style="padding: 10px 0;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                    <input type="text" name="title" placeholder="Titre de la vidéo" required>
                    <input type="file" name="file" required>
                    <button type="submit">Uploader & Publier</button>
                </form>

                <h3>UTILITAIRE</h3>
                <form class="util-form" method="POST" action="{{ url_for('convert_gif') }}" enctype="multipart/form-data" style="padding: 10px 0;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                    <input type="file" name="gif_file" accept=".gif" required>
                    <button type="submit" style="background-color: #9B59B6;">Convertir GIF -> PNG</button>
                </form>
                
                <h3>GESTION AMIS</h3>
                <form class="friend-form" method="POST" action="{{ url_for('add_friend') }}" style="padding: 10px 0;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                    <input type="text" name="friend_username" placeholder="Pseudo de l'ami" required>
                    <button type="submit" style="background-color: #2ECC71;">Ajouter Ami</button>
                </form>

                <form method="POST" action="{{ url_for('logout') }}" style="margin-top: 20px; padding: 10px 0;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                    <button type="submit" class="logout-button">Déconnexion</button>
                </form>

            {% else %}
                <h3>CONNEXION / INSCRIPTION</h3>
                <form class="auth-form" method="POST" action="{{ url_for('register') }}" style="padding: 10px 0;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                    <input type="email" name="email" placeholder="Email" required>
                    <input type="text" name="username" placeholder="Pseudo" required>
                    <input type="password" name="password" placeholder="Mot de passe" required>
                    <button type="submit">S'inscrire</button>
                </form>
                <form class="auth-form" method="POST" action="{{ url_for('login') }}" style="padding: 10px 0;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                    <input type="text" name="username" placeholder="Pseudo" required>
                    <input type="password" name="password" placeholder="Mot de passe" required>
                    <button type="submit">Connexion</button>
                </form>
            {% endif %}
        </div>
        
        <div class="content-area">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash {{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}

            {% if user_username %}

                <h2 class="section-title">En Tendances (Vidéos Publiées)</h2>
                <div class="video-grid">
                    {% for video in uploaded_videos | reverse %}
                        <div class="video-item">
                            <div class="thumbnail-placeholder">
                                <img src="data:image/svg+xml;charset=UTF-8,%3Csvg%20width%3D%22300%22%20height%3D%22180%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%20300%20180%22%20preserveAspectRatio%3D%22none%22%3E%3Crect%20width%3D%22300%22%20height%3D%22180%22%20fill%3D%22%23303030%22%3E%3C%2Frect%3E%3Ctext%20x%3D%2250%25%22%20y%3D%2250%25%22%20fill%3D%22%23AAAAAA%22%20font-family%3D%22sans-serif%22%20font-size%3D%2218%22%20text-anchor%3D%22middle%22%3E{{ video.title }}%3C%2Ftext%3E%3C%2Fsvg%3E" alt="Miniature">
                            </div>
                            <div class="video-details">
                                <div class="channel-icon"></div>
                                <div class="video-info">
                                    <h4>{{ video.title }}</h4>
                                    <p>@{{ video.user }}</p>
                                    <p>{{ video.date }} | Statut: {{ video.status }}</p>
                                    {% if video.status == 'Converti (Simulé)' %}
                                        <div class="video-status-download">
                                            <a href="{{ url_for('download_file', filename=video.converted_filename) }}" download>Télécharger</a>
                                        </div>
                                    {% endif %}
                                </div>
                            </div>
                        </div>
                    {% endfor %}
                    {% if not uploaded_videos %}
                        <p style="font-size: small; color: #AAAAAA;">Aucune vidéo publiée. Utilisez le menu latéral pour uploader votre propre vidéo.</p>
                    {% endif %}
                </div>
                
                <h2 class="section-title" style="margin-top: 50px;">🖼️ Conversions GIF récentes</h2>
                <div class="image-grid">
                    {% for img in uploaded_images | reverse %}
                        <div class="image-item">
                            <img src="{{ url_for('download_converted_image', filename=img.filename) }}" alt="Image convertie">
                            <a href="{{ url_for('download_converted_image', filename=img.filename) }}" download>Télécharger {{ img.format }}</a>
                        </div>
                    {% endfor %}
                </div>
                
                <div class="chat-container">
                    <h2 class="section-title">💬 Messagerie Privée (Amis uniquement)</h2>
                    <p style="font-size: small; color: #AAAAAA; margin-bottom: 10px;">Amis : {% for friend_name in friend_names %}@{{ friend_name }}{% if not loop.last %}, {% endif %}{% endfor %}</p>
                    <div class="chat-box" id="messages">
                        {% for msg in chat_messages %}
                            <div class="message"><span class="user-pseudo">@{{ msg.user }}</span>: {{ msg.text }}</div>
                        {% endfor %}
                    </div>
                    <div class="message-input">
                        <input type="text" id="message_input" placeholder="Envoyer un message à vos amis...">
                        <button onclick="sendMessage()">Envoyer</button>
                    </div>
                </div>

                <script>
                    var socket = io();
                    var user_username = "{{ user_username }}";

                    // --- Réception de messages ---
                    socket.on('broadcast_message', function(data) {
                        var messagesDiv = document.getElementById('messages');
                        var div = document.createElement('div');
                        div.className = 'message';
                                        
                        if (data.user === 'Système') {
                            div.innerHTML = '<span style="font-weight: 700; color: #FF0000;">[' + data.user + ']</span>: ' + data.text;
                        } else {
                            div.innerHTML = '<span class="user-pseudo">@' + data.user + '</span>: ' + data.text;
                        }
                        
                        messagesDiv.appendChild(div);
                        messagesDiv.scrollTop = messagesDiv.scrollHeight;
                    });

                    // --- Envoi de messages ---
                    function sendMessage() {
                        var input = document.getElementById('message_input');
                        var content = input.value;

                        if (content && user_username) {
                            socket.emit('new_message', {
                                user: user_username,
                                text: content
                            });
                            input.value = '';
                        }
                    }

                    // Envoyer avec la touche Entrée
                    document.getElementById('message_input').addEventListener('keypress', function(e) {
                        if (e.key === 'Enter') {
                            sendMessage();
                        }
                    });

                    // Scroll au bas au chargement
                    document.addEventListener('DOMContentLoaded', (event) => {
                        var messagesDiv = document.getElementById('messages');
                        if (messagesDiv) {
                            messagesDiv.scrollTop = messagesDiv.scrollHeight;
                        }
                    });
                </script>

            {% else %}
                <h1 style="text-align: center; color: #FFFFFF; margin-top: 50px;">Bienvenue sur YouTube Social Python!</h1>
                <p style="text-align: center; color: #AAAAAA; margin-top: 20px;">Utilisez le panneau de gauche pour vous inscrire ou vous connecter.</p>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""

# --------------------------
# 4. FONCTIONS UTILITAIRES ET DE SÉCURITÉ
# --------------------------

def generate_unique_filename(extension):
    """Génère un nom de fichier unique."""
    return f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{random.randint(1000, 9999)}.{extension}"

def convert_to_mp4(input_path, output_dir):
    """Fonction de conversion DE-ACTIVÉE / SIMULÉE."""
    # SIMULATION: Crée un fichier placeholder pour démontrer le workflow sans utiliser FFmpeg.
    print("ATTENTION: La conversion FFmpeg est désactivée. Retourne un fichier de test.")
    
    simulated_filename = "simulated_video_" + generate_unique_filename("mp4")
    try:
        # Crée un petit fichier vide/placeholder dans le répertoire "converted"
        with open(os.path.join(output_dir, simulated_filename), 'w') as f:
            f.write(f"Ceci est un fichier vidéo simulé converti à partir de {os.path.basename(input_path)}.")
    except Exception as e:
        print(f"Erreur lors de la création du fichier simulé: {e}")
        return None
    finally:
        # Tente de supprimer le fichier original téléchargé après la simulation
        if os.path.exists(input_path):
             os.remove(input_path) 
        
    return simulated_filename

def check_csrf_token(request):
    """Vérifie si le jeton CSRF est valide (sécurité anti-bot)."""
    return request.form.get('csrf_token') == session.get('csrf_token')


# --------------------------
# 5. ROUTES FLASK
# --------------------------

@app.route('/', methods=['GET'])
def index():
    # 🍪 1. Gestion du Jeton CSRF
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(16)
        
    current_username = session.get('user_username')
    friend_names = []
    
    if current_username:
        with app.app_context():
            current_user = User.query.filter_by(username=current_username).first()
            if current_user:
                # Récupère la liste des amis pour l'affichage
                friend_names = [f.username for f in current_user.friends.all()]

    return render_template_string(
        HTML_TEMPLATE,
        user_username=current_username,
        chat_messages=chat_messages,
        uploaded_videos=uploaded_videos,
        uploaded_images=uploaded_images,
        friend_names=friend_names,
        csrf_token=session['csrf_token'] 
    )

@app.route('/register', methods=['POST'])
def register():
    # 🔒 2. Vérification du jeton CSRF
    if not check_csrf_token(request):
        flash('Erreur de sécurité: Jeton invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('index'))
        
    email = request.form['email']
    username = request.form['username']
    password = request.form['password']

    with app.app_context():
        if User.query.filter_by(email=email).first():
            flash('Cet email est déjà enregistré.', 'error')
            return redirect(url_for('index'))
        
        if User.query.filter_by(username=username).first():
            flash('Ce pseudo est déjà utilisé.', 'error')
            return redirect(url_for('index'))

        new_user = User(email=email, username=username)
        new_user.set_password(password)
        
        db.session.add(new_user)
        db.session.commit()
        
        session['user_username'] = username
        flash(f'Compte créé et connexion réussie pour @{username}!', 'success')
        return redirect(url_for('index'))

@app.route('/login', methods=['POST'])
def login():
    # 🔒 2. Vérification du jeton CSRF
    if not check_csrf_token(request):
        flash('Erreur de sécurité: Jeton invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('index'))
        
    username = request.form['username']
    password = request.form['password']

    with app.app_context():
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            session['user_username'] = username
            flash(f'Connexion réussie pour @{username}!', 'success')
        else:
            flash('Pseudo ou mot de passe incorrect.', 'error')
            
        return redirect(url_for('index'))

@app.route('/logout', methods=['POST'])
def logout():
    # 🔒 2. Vérification du jeton CSRF
    if not check_csrf_token(request):
        flash('Erreur de sécurité: Jeton invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('index'))
        
    session.pop('user_username', None)
    flash('Vous êtes déconnecté.', 'success')
    return redirect(url_for('index'))

@app.route('/upload', methods=['POST'])
def upload_file():
    # 🔒 2. Vérification du jeton CSRF
    if not check_csrf_token(request):
        flash('Erreur de sécurité: Jeton invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('index'))

    if 'user_username' not in session:
        flash('Veuillez vous connecter pour publier du contenu.', 'error')
        return redirect(url_for('index'))

    if 'file' not in request.files:
        flash('Aucun fichier sélectionné.', 'error')
        return redirect(url_for('index'))

    file = request.files['file']
    title = request.form.get('title', 'Vidéo sans titre')

    if file.filename == '':
        flash('Nom de fichier invalide.', 'error')
        return redirect(url_for('index'))

    if file:
        # Sécurisation du nom de fichier pour éviter les attaques de chemin
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        try:
            # Sauvegarde du fichier temporairement dans /uploads
            file.save(file_path)
            
            # --- CONVERSION (SIMULÉE) ---
            converted_filename = convert_to_mp4(file_path, app.config['CONVERTED_FOLDER'])
            
            if converted_filename:
                uploaded_videos.append({
                    'title': title,
                    'converted_filename': converted_filename,
                    'date': datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    'user': session['user_username'],
                    'status': 'Converti (Simulé)'
                })
                flash(f'"{title}" a été simulé et publié !', 'success')
            else:
                flash(f'Échec de la simulation de conversion.', 'error')

        except Exception as e:
            flash(f"Erreur lors de l'enregistrement ou la simulation: {e}", 'error')

        return redirect(url_for('index'))
    
    flash('Erreur lors de l\'upload du fichier.', 'error')
    return redirect(url_for('index'))


@app.route('/convert_gif', methods=['POST'])
def convert_gif():
    # 🔒 2. Vérification du jeton CSRF
    if not check_csrf_token(request):
        flash('Erreur de sécurité: Jeton invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('index'))

    if 'user_username' not in session:
        flash('Veuillez vous connecter pour utiliser le convertisseur.', 'error')
        return redirect(url_for('index'))

    if 'gif_file' not in request.files:
        flash('Aucun fichier GIF sélectionné.', 'error')
        return redirect(url_for('index'))

    file = request.files['gif_file']
    if not file.filename or not file.filename.lower().endswith('.gif'):
        flash("Seuls les fichiers GIF sont supportés.", 'error')
        return redirect(url_for('index'))

    try:
        # Enregistrer le fichier GIF temporairement
        gif_filename = secure_filename(file.filename)
        gif_path = os.path.join(app.config['UPLOAD_FOLDER'], gif_filename)
        file.save(gif_path)

        # --- CONVERSION AVEC PILLOW (Légère) ---
        output_filename = generate_unique_filename("png")
        output_path = os.path.join(app.config['CONVERTED_FOLDER'], output_filename)
        
        img = Image.open(gif_path)
        img.seek(0) # Prend la première image du GIF (car un GIF est une séquence d'images)
        img.save(output_path, 'PNG')
        
        # Suppression du GIF original temporaire
        os.remove(gif_path) 
        
        uploaded_images.append({
            'filename': output_filename,
            'format': 'PNG',
            'user': session['user_username']
        })
        flash(f'Conversion GIF -> PNG réussie! Téléchargez l\'image.', 'success')

    except Exception as e:
        flash(f"Erreur de conversion GIF : {e}", 'error')

    return redirect(url_for('index'))


@app.route('/download/<filename>')
def download_file(filename):
    """Permet de télécharger les fichiers convertis (vidéos simulées)."""
    return send_from_directory(app.config['CONVERTED_FOLDER'], filename, as_attachment=True)

@app.route('/converted_images/<filename>')
def download_converted_image(filename):
    """Affiche les images converties (GIF)."""
    return send_from_directory(app.config['CONVERTED_FOLDER'], filename)


@app.route('/add_friend', methods=['POST'])
def add_friend():
    # 🔒 2. Vérification du jeton CSRF
    if not check_csrf_token(request):
        flash('Erreur de sécurité: Jeton invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('index'))
        
    if 'user_username' not in session:
        flash('Veuillez vous connecter pour ajouter des amis.', 'error')
        return redirect(url_for('index'))
    
    friend_username = request.form['friend_username']
    current_username = session['user_username']

    if friend_username == current_username:
        flash("Vous ne pouvez pas vous ajouter vous-même.", 'error')
        return redirect(url_for('index'))
    
    with app.app_context():
        current_user = User.query.filter_by(username=current_username).first()
        friend_user = User.query.filter_by(username=friend_username).first()

        if not friend_user:
            flash(f"Le pseudo @{friend_username} n'existe pas.", 'error')
        elif current_user.is_friend(friend_user):
            flash(f"@{friend_username} est déjà dans votre liste d'amis.", 'info')
        else:
            current_user.add_friend(friend_user)
            db.session.commit()
            flash(f"@{friend_username} a été ajouté à vos amis!", 'success')
            
    return redirect(url_for('index'))


# --------------------------
# 6. SOCKETIO (CHAT PRIVÉ)
# --------------------------

@socketio.on('connect')
def handle_connect():
    current_username = session.get('user_username')
    if current_username:
        with app.app_context():
            user = User.query.filter_by(username=current_username).first()
            if user:
                # Stocke l'ID du socket pour l'envoi de messages privés
                user_sid_map[user.id] = request.sid
                print(f"User @{current_username} connected with SID: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    current_username = session.get('user_username')
    if current_username:
        with app.app_context():
            user = User.query.filter_by(username=current_username).first()
            # Supprime l'utilisateur de la map s'il est déconnecté
            if user and user.id in user_sid_map and user_sid_map[user.id] == request.sid:
                del user_sid_map[user.id]
                print(f"User @{current_username} disconnected.")


@socketio.on('new_message')
def handle_new_message(data):
    """
    Réceptionne le message et l'émet UNIQUEMENT aux amis connectés.
    """
    user_username = session.get('user_username', 'Anonyme')
    text = data.get('text', '...')
    
    if text and user_username != 'Anonyme':
        with app.app_context():
            sender = User.query.filter_by(username=user_username).first()
            
            if not sender:
                return 

            message_data = {'user': user_username, 'text': text}
            chat_messages.append(message_data)
            
            # 1. Émettre le message à l'expéditeur lui-même (confirmation)
            emit('broadcast_message', message_data, room=request.sid)

            # 2. Émettre le message à chaque ami connecté
            friends_list = sender.friends.all()

            for friend in friends_list:
                friend_sid = user_sid_map.get(friend.id)

                if friend_sid:
                    # Émet le message uniquement au socket de cet ami
                    emit('broadcast_message', message_data, room=friend_sid)
                    print(f"Message de @{user_username} envoyé à @{friend.username}.")
    else:
        # Message d'erreur à l'expéditeur
        error_data = {'user': 'Système', 'text': 'Veuillez vous connecter pour parler.'}
        emit('broadcast_message', error_data, room=request.sid)


# --------------------------
# 7. LANCEMENT 
# --------------------------

# IMPORTANT : Pour Render, nous utilisons Gunicorn pour servir l'application. 
# La commande de démarrage sur Render sera généralement: `gunicorn app:app` (ou `gunicorn app:socketio.server` si vous utilisez Flask-SocketIO)
# Le worker doit être compatible (ex: gevent ou eventlet) si vous utilisez SocketIO en production.

if __name__ == '__main__':
    PORT_CHOISI = 5003 
    # Le mode debug=True n'est pas utilisé en production sur Render
    socketio.run(app, debug=True, port=PORT_CHOISI)