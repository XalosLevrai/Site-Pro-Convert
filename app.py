from flask import Flask, render_template_string, request, redirect, url_for, flash, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename
import os
import datetime
import random
import string
import yt_dlp
# import ffmpeg  # <-- DÉSACTIVÉ
from werkzeug.security import generate_password_hash, check_password_hash

# --------------------------
# 1. INITIALISATION ET CONFIG
# --------------------------

app = Flask(__name__)

# LECTURE DE LA CLÉ SECRÈTE DEPUIS L'ENVIRONNEMENT
app.config['SECRET_KEY'] = os.environ.get(
    'SECRET_KEY', 
    'cle_secrete_de_secours_a_ne_pas_utiliser_en_prod'
)

# Configuration de la base de données : UTILISATION FORCÉE DE POSTGRESQL
# ---------------------------------------------------------------------
# !!! REMPLACER PAR VOTRE URL POSTGRES COMPLÈTE (COMMENCE PAR postgres://) !!!
HARDCODED_DATABASE_URL = "postgresql://pro_convert_db_user:haM3FpLxeoXTlB3lIDobF6tSnYgBHjQX@dpg-d4u4p015pdvs73bnebjg-a.virginia-postgres.render.com/pro_convert_db" 
# ---------------------------------------------------------------------

database_url = HARDCODED_DATABASE_URL

# --- CORRECTION CRUCIALE POUR RENDER / SQLAlchemy ---
# Si l'URL de connexion est fournie par Render au format 'postgres://', 
# SQLAlchemy (avec psycopg2) a besoin de 'postgresql://'.
if database_url and database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False 

# Dossiers d'uploads
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # Limite d'upload à 100MB

db = SQLAlchemy(app)
socketio = SocketIO(app)

# Créer les dossiers nécessaires s'ils n'existent pas
for folder in [app.config['UPLOAD_FOLDER'], 'converted']:
    if not os.path.exists(folder):
        os.makedirs(folder)

# Listes temporaires pour le contenu non stocké en DB
chat_messages = []
uploaded_videos = []

# --------------------------
# 2. MODÈLES DE BASE DE DONNÉES (PSEUDO, EMAIL ET AMIS)
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
    password = db.Column(db.String(128), nullable=False) 

    # Relation d'Amis
    friends = db.relationship(
        'User', 
        secondary=friends,
        primaryjoin=(friends.c.user_id == id),
        secondaryjoin=(friends.c.friend_id == id),
        backref=db.backref('friend_of', lazy='dynamic'),
        lazy='dynamic'
    )
    
    def set_password(self, password):
        self.password = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password, password)

    def add_friend(self, user):
        if not self.is_friend(user):
            self.friends.append(user)
            user.friends.append(self) 

    def is_friend(self, user):
        with app.app_context():
            return db.session.query(friends).filter(
                friends.c.user_id == self.id, 
                friends.c.friend_id == user.id
            ).count() > 0

    def __repr__(self):
        return f"User('{self.username}')"

# Création des tables de base de données si elles n'existent pas
with app.app_context():
    db.create_all()

# --------------------------
# 3. LE CODE HTML/CSS/JS INTÉGRÉ (Non modifié)
# --------------------------

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Mon Réseau Social Python</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        /* CSS V3: Plus Stylé et Moderne */
        @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;700&display=swap');
        body { font-family: 'Roboto', sans-serif; margin: 0; padding: 0; background-color: #f0f2f5; color: #1c1e21; }
        .container { display: flex; max-width: 1400px; margin: 40px auto; background: #ffffff; border-radius: 18px; box-shadow: 0 12px 24px rgba(0, 0, 0, 0.1); min-height: 85vh; overflow: hidden; }
        .sidebar { width: 320px; background-color: #294a73; color: white; padding: 30px; box-sizing: border-box; display: flex; flex-direction: column; justify-content: space-between; }
        .main-content { flex-grow: 1; padding: 30px; }
        h1, h2, h3 { color: #294a73; margin-bottom: 15px; }
        h2 { border-bottom: 2px solid #5a82a0; padding-bottom: 10px; }
        
        /* Formulaires */
        .auth-form input, .upload-form input, .friend-form input { width: 100%; padding: 14px; margin-bottom: 15px; border: none; border-radius: 8px; box-shadow: inset 0 1px 3px rgba(0,0,0,0.1); background: #f7f7f7; font-size: 16px; }
        .auth-form button, .upload-form button, .friend-form button, .reset-button { width: 100%; padding: 14px; background-color: #4CAF50; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 16px; font-weight: 700; transition: background-color 0.3s; margin-top: 5px;}
        .auth-form button:hover, .upload-form button:hover, .friend-form button:hover, .reset-button:hover { background-color: #45a049; }
        
        /* Connexion/Déconnexion */
        .sidebar p strong { color: #ffeb3b; font-size: 1.1em; }
        .sidebar .logout-button { background-color: #e74c3c; margin-top: 20px; }
        .sidebar .logout-button:hover { background-color: #c0392b; }

        /* Messages Flash */
        .flash { padding: 15px; margin-bottom: 20px; border-radius: 8px; font-weight: bold; }
        .success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .info { background-color: #cce5ff; color: #004085; border: 1px solid #b8daff; }

        /* Chat */
        .chat-box { height: 400px; border: 1px solid #ddd; overflow-y: scroll; padding: 15px; margin-bottom: 15px; background-color: #fcfcfc; border-radius: 8px; }
        .message-input { display: flex; }
        .message-input input { flex-grow: 1; margin-right: 10px; }
        .user-pseudo { font-weight: 700; color: #3498db; margin-right: 8px; }

        /* Vidéos */
        .video-grid { display: flex; flex-wrap: wrap; gap: 25px; margin-top: 25px; }
        .video-item { width: calc(33.333% - 17px); background: #f9f9f9; border-radius: 10px; overflow: hidden; box-shadow: 0 4px 8px rgba(0,0,0,0.05); }
        .video-item img { width: 100%; height: 180px; object-fit: cover; background-color: #34495e; display: block; border-bottom: 2px solid #5a82a0; }
        .video-details { padding: 15px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="sidebar">
            <div>
                <h2 style="color: #ffeb3b; border-bottom-color: #5a82a0;">Hub Social Python</h2>
                {% if user_username %}
                    <p style="margin-top: 10px;">Connecté en tant que: <br><strong style="color: #ffeb3b;">@{{ user_username }}</strong></p>
                    
                    <h3 style="color: white; border-bottom: 1px solid #5a82a0; padding-bottom: 8px; margin-top: 25px;">Ajouter un Ami</h3>
                    <form class="friend-form" method="POST" action="{{ url_for('add_friend') }}">
                        <input type="text" name="friend_username" placeholder="Pseudo de l'ami" required>
                        <button type="submit">Ajouter l'Ami</button>
                    </form>

                    <h3 style="color: white; border-bottom: 1px solid #5a82a0; padding-bottom: 8px; margin-top: 25px;">Publier (Vidéo)</h3>
                    <form class="upload-form" method="POST" action="{{ url_for('upload_file') }}" enctype="multipart/form-data">
                        <input type="text" name="title" placeholder="Titre de la vidéo" required>
                        <input type="file" name="file" required>
                        <button type="submit">Uploader Vidéo</button>
                    </form>
                    
                    <h3 style="color: white; border-bottom: 1px solid #5a82a0; padding-bottom: 8px; margin-top: 25px;">Mot de Passe</h3>
                    <p style="font-size: small; color: #bdc3c7;">
                        <button class="reset-button" style="background-color: #f39c12;" onclick="document.getElementById('reset-form').style.display='block'">Réinitialiser</button>
                    </p>
                    <form id="reset-form" method="POST" action="{{ url_for('forgot_password') }}" style="display:none; margin-top: 10px;">
                        <input type="email" name="email" placeholder="Votre Email" required>
                        <button type="submit">Envoyer Lien</button>
                    </form>

                {% else %}
                    <h3 style="color: white;">Créer un compte</h3>
                    <form class="auth-form" method="POST" action="{{ url_for('register') }}">
                        <input type="email" name="email" placeholder="Email (Unique)" required>
                        <input type="text" name="username" placeholder="Pseudo (Unique)" required>
                        <input type="password" name="password" placeholder="Mot de passe" required>
                        <button type="submit">S'inscrire</button>
                    </form>
                    <h3 style="color: white; margin-top: 25px;">Se connecter</h3>
                    <form class="auth-form" method="POST" action="{{ url_for('login') }}">
                        <input type="text" name="username" placeholder="Pseudo" required>
                        <input type="password" name="password" placeholder="Mot de passe" required>
                        <button type="submit">Connexion</button>
                    </form>
                {% endif %}
            </div>

            {% if user_username %}
            <form method="POST" action="{{ url_for('logout') }}" style="margin-top: 20px;">
                <button type="submit" class="logout-button">Déconnexion</button>
            </form>
            {% endif %}
        </div>
        
        <div class="main-content">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash {{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}

            {% if user_username %}
                <h2>👥 Amis Connectés</h2>
                <p>Liste des amis : {% for friend_name in friend_names %}@{{ friend_name }}{% if not loop.last %}, {% endif %}{% endfor %}</p>
                {% if not friend_names %}<p style="font-size: small; color: #7f8c8d;">Ajoutez des amis via la barre latérale.</p>{% endif %}

                <h2 style="margin-top: 40px;">💬 Messagerie Temps Réel</h2>
                <div class="chat-box" id="messages">
                    {% for msg in chat_messages %}
                        <div class="message"><span class="user-pseudo">@{{ msg.user }}</span>: {{ msg.text }}</div>
                    {% endfor %}
                </div>
                <div class="message-input">
                    <input type="text" id="message_input" placeholder="Envoyer un message texte (pas vocal/appel)">
                    <button onclick="sendMessage()">Envoyer</button>
                </div>
                
                <h2 style="margin-top: 40px;">📼 Fil d'Actualité Vidéo (Fonctionnalité désactivée)</h2>
                <div class="video-grid">
                    {% for video in uploaded_videos %}
                        <div class="video-item">
                            <img src="data:image/svg+xml;charset=UTF-8,%3Csvg%20width%3D%22300%22%20height%3D%22180%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%20300%20180%22%20preserveAspectRatio%3D%22none%22%3E%3Crect%20width%3D%22300%22%20height%3D%22180%22%20fill%3D%22%23294a73%22%3E%3C%2Frect%3E%3Ctext%20x%3D%2250%25%22%20y%3D%2250%25%22%20fill%3D%22%23f0f2f5%22%20font-family%3D%22sans-serif%22%20font-size%3D%2220%22%20text-anchor%3D%22middle%22%3E{{ video.title }}%3C%2Ftext%3E%3C%2Fsvg%3E" alt="Miniature">
                            <div class="video-details">
                                <h4>{{ video.title }}</h4>
                                <p style="font-size: small; color: #7f8c8d;">@{{ video.user }} | {{ video.date }}</p>
                                <p style="font-size: small; color: #7f8c8d;">Statut : {{ video.status }}</p>
                                {% if video.status == 'Converti' %}
                                    <a href="{{ url_for('download_file', filename=video.converted_filename) }}" download style="color: #4CAF50;">Télécharger (Simulé)</a>
                                {% endif %}
                            </div>
                        </div>
                    {% endfor %}
                    {% if not uploaded_videos %}
                        <p style="font-size: small; color: #7f8c8d;">La fonctionnalité de conversion vidéo est désactivée pour le déploiement. Tentez l'inscription !</p>
                    {% endif %}
                </div>

                <script>
                    var socket = io();
                    var user_username = "{{ user_username }}";

                    // --- Réception de messages ---
                    socket.on('broadcast_message', function(data) {
                        var messagesDiv = document.getElementById('messages');
                        var div = document.createElement('div');
                        div.className = 'message';
                        div.innerHTML = '<span class="user-pseudo">@' + data.user + '</span>: ' + data.text;
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
                <h2 style="text-align: center; color: #294a73;">Bienvenue sur votre Réseau Social Python!</h2>
                <p style="text-align: center; color: #7f8c8d; margin-top: 20px;">Veuillez vous inscrire avec votre email et un pseudo unique pour commencer à interagir.</p>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""

# --------------------------
# 4. FONCTIONS DE CONVERSION (SIMPLIFIÉES/DÉSACTIVÉES)
# --------------------------

def generate_unique_filename(extension):
    """Génère un nom de fichier unique."""
    return f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{random.randint(1000, 9999)}.{extension}"

def convert_to_mp4(input_path, output_dir):
    """Fonction de conversion DE-ACTIVÉE pour le déploiement sur Render."""
    print("ATTENTION: FFmpeg est désactivé. Retourne un fichier de test.")
    return "simulated_video.mp4" 

# --------------------------
# 5. ROUTES FLASK (LOGIQUE MISE À JOUR)
# --------------------------

@app.route('/', methods=['GET'])
def index():
    current_username = session.get('user_username')
    friend_names = []
    
    if current_username:
        with app.app_context():
            current_user = User.query.filter_by(username=current_username).first()
            if current_user:
                friend_names = [f.username for f in current_user.friends.all()]

    return render_template_string(
        HTML_TEMPLATE,
        user_username=current_username,
        chat_messages=chat_messages,
        uploaded_videos=uploaded_videos,
        friend_names=friend_names
    )

@app.route('/register', methods=['POST'])
def register():
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
        session['user_email'] = email
        flash(f'Compte créé et connexion réussie pour @{username}!', 'success')
        return redirect(url_for('index'))

@app.route('/login', methods=['POST'])
def login():
    username = request.form['username']
    password = request.form['password']

    with app.app_context():
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            session['user_username'] = username
            session['user_email'] = user.email
            flash(f'Connexion réussie pour @{username}!', 'success')
        else:
            flash('Pseudo ou mot de passe incorrect.', 'error')
            
        return redirect(url_for('index'))

@app.route('/logout', methods=['POST'])
def logout():
    session.pop('user_username', None)
    session.pop('user_email', None)
    flash('Vous êtes déconnecté.', 'success')
    return redirect(url_for('index'))

@app.route('/upload', methods=['POST'])
def upload_file():
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
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        try:
            file.save(file_path)
            
            # --- CONVERSION (SIMULÉE) ---
            flash(f'Fichier "{title}" téléchargé. Conversion SIMULÉE...', 'info')
            converted_filename = convert_to_mp4(file_path, 'converted')
            
            if converted_filename:
                # Enregistrement dans la liste pour l'affichage
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
            flash(f"Erreur lors de l'enregistrement : {e}", 'error')

        return redirect(url_for('index'))
    
    flash('Erreur lors de l\'upload du fichier.', 'error')
    return redirect(url_for('index'))


@app.route('/download/<filename>')
def download_file(filename):
    """Permet de télécharger les fichiers convertis (simulé)."""
    return send_from_directory('converted', filename, as_attachment=True)


@app.route('/add_friend', methods=['POST'])
def add_friend():
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


@app.route('/forgot_password', methods=['POST'])
def forgot_password():
    email = request.form['email']
    
    with app.app_context():
        user = User.query.filter_by(email=email).first()

        if user:
            print(f"\n--- SIMULATION EMAIL (MOT DE PASSE OUBLIÉ) ---")
            print(f"DESTINATAIRE : {email}")
            print(f"----------------------------------------------\n")
            
            flash('Un lien de réinitialisation de mot de passe a été (simulé) envoyé à votre email.', 'info')
        else:
            flash("Aucun compte trouvé avec cet email.", 'error')
            
        return redirect(url_for('index'))


# --------------------------
# 6. SOCKETIO (CHAT EN TEMPS RÉEL)
# --------------------------

@socketio.on('new_message')
def handle_new_message(data):
    user = session.get('user_username', 'Anonyme')
    text = data.get('text', '...')
    
    if text and user != 'Anonyme':
        message_data = {'user': user, 'text': text}
        chat_messages.append(message_data)
        
        emit('broadcast_message', message_data, broadcast=True)

# --------------------------
# 7. LANCEMENT DU SERVEUR
# --------------------------

if __name__ == '__main__':
    PORT_CHOISI = 5003
    socketio.run(app, debug=True, port=PORT_CHOISI)