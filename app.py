from flask import Flask, render_template_string, request, redirect, url_for, flash, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import os
import datetime
import random
import string
import yt_dlp
# import ffmpeg  # <-- DÉSACTIVÉ TEMPORAIREMENT

# --------------------------
# 1. INITIALISATION ET CONFIG
# --------------------------

app = Flask(__name__)

# LECTURE DE LA CLÉ SECRÈTE DEPUIS L'ENVIRONNEMENT
app.config['SECRET_KEY'] = os.environ.get(
    'SECRET_KEY', 
    'cle_secrete_de_secours_a_ne_pas_utiliser_en_prod'
)

# VOTRE URL POSTGRES COPIÉE DE RENDER
RAW_DATABASE_URL = 'postgresql://pro_convert_db_user:haM3FpLxeoXTlB3lIDobF6tSnYgBHjQX@dpg-d4u4p015pdvs73bnebjg-a.virginia-postgres.render.com/pro_convert_db' 

# Correction du format de l'URL
if RAW_DATABASE_URL.startswith('postgres://'):
    database_url = RAW_DATABASE_URL.replace('postgres://', 'postgresql://', 1)
else:
    database_url = RAW_DATABASE_URL

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False 

# Dossiers d'uploads
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # Limite d'upload à 100MB

db = SQLAlchemy(app)
socketio = SocketIO(app)

# Créer les dossiers nécessaires s'ils n'existent pas
for folder in [app.config['UPLOAD_FOLDER'], 'converted']:
    if not os.path.exists(folder):
        os.makedirs(folder)

# Listes temporaires pour le contenu non stocké en DB (non persistants après redémarrage)
chat_messages = []
uploaded_videos = []

# Table pour garder la trace des utilisateurs connectés et de leur ID Socket
# Format : {user_id: socket_id}
user_sid_map = {} 

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
    # Taille augmentée pour le hash de mot de passe
    password = db.Column(db.String(256), nullable=False) 

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

# --------------------------
# CORRECTION DÉPLOIEMENT : CRÉATION DE TABLES FORCÉE
# --------------------------
with app.app_context():
    print("Tentative de CRÉATION des tables via app_context (fix UndefinedTable)...")
    try:
        db.create_all() 
        print("Tables de la base de données créées/vérifiées avec succès.")
    except Exception as e:
        print(f"Échec de la création des tables lors du démarrage: {e}")
# --------------------------


# --------------------------
# 3. LE CODE HTML/CSS/JS INTÉGRÉ (Interface utilisateur - STYLE YOUTUBE)
# --------------------------

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>YouTube Python Social</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">
    <style>
        /* Palette de couleurs YouTube: #282828 (Fonds sombres), #FFFFFF (Texte), #FF0000 (Rouge/Action) */
        @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap');
        body { font-family: 'Roboto', sans-serif; margin: 0; padding: 0; background-color: #181818; color: #FFFFFF; }
        
        /* Header (Style YouTube Top Bar) */
        .header { background-color: #202020; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #303030; }
        .logo { font-size: 24px; font-weight: 700; color: #FFFFFF; }
        .logo span { color: #FF0000; margin-left: -4px; } /* Pour le style "YouTUBE" */

        /* Conteneur principal */
        .main-layout { display: flex; max-width: 1600px; margin: 0 auto; }

        /* Sidebar (Navigation/Connexion) */
        .sidebar { width: 240px; background-color: #282828; padding: 20px 10px; box-sizing: border-box; height: 100vh; position: sticky; top: 0; border-right: 1px solid #303030; }
        .sidebar h3 { color: #AAAAAA; font-size: 14px; margin-top: 20px; padding-bottom: 5px; border-bottom: 1px solid #303030; }
        .sidebar-item { padding: 10px 15px; border-radius: 5px; cursor: pointer; display: flex; align-items: center; font-size: 14px; transition: background-color 0.2s; }
        .sidebar-item:hover { background-color: #383838; }
        .sidebar-item .material-icons { margin-right: 15px; font-size: 20px; color: #909090; }
        .sidebar p strong { color: #00AFFF; font-size: 1em; }


        /* Contenu Principal (Fil d'Actualité) */
        .content-area { flex-grow: 1; padding: 20px; }
        
        /* Grille de Vidéos (YouTube Grid) */
        .video-grid { 
            display: grid; 
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); 
            gap: 20px; 
            margin-top: 20px; 
        }
        .video-item { color: #FFFFFF; }
        .thumbnail-placeholder { width: 100%; height: 180px; background-color: #303030; display: flex; align-items: center; justify-content: center; border-radius: 8px; margin-bottom: 10px; position: relative; overflow: hidden;}
        .thumbnail-placeholder text { fill: #AAAAAA; font-size: 18px; }
        .video-details { display: flex; }
        .video-info { margin-left: 10px; }
        .video-info h4 { font-size: 16px; font-weight: 500; margin: 0 0 5px 0; line-height: 1.3; }
        .video-info p { font-size: 12px; color: #AAAAAA; margin: 0; }
        .channel-icon { width: 36px; height: 36px; background: #FF0000; border-radius: 50%; flex-shrink: 0; }

        /* Chat Box (Style Dark Mode) */
        .chat-container { margin-top: 40px; padding-top: 20px; border-top: 1px solid #303030; }
        .chat-box { height: 300px; border: 1px solid #404040; overflow-y: scroll; padding: 15px; margin-bottom: 15px; background-color: #202020; border-radius: 8px; }
        .message { margin-bottom: 8px; }
        .user-pseudo { font-weight: 500; color: #4CAF50; margin-right: 8px; } /* Vert pour les utilisateurs */
        .message-input { display: flex; }
        .message-input input { flex-grow: 1; margin-right: 10px; background: #303030; border: 1px solid #404040; color: #FFFFFF; padding: 10px; border-radius: 4px; }
        .message-input button { background-color: #FF0000; color: white; border: none; padding: 10px 15px; border-radius: 4px; cursor: pointer; transition: background-color 0.2s; }
        .message-input button:hover { background-color: #CC0000; }


        /* Formulaires et Boutons d'Action (Sidebar) */
        .auth-form input, .upload-form input, .friend-form input { width: 100%; padding: 10px; margin-bottom: 10px; border: 1px solid #404040; border-radius: 4px; background: #303030; color: #FFFFFF; }
        .auth-form button, .upload-form button, .friend-form button, .reset-button { width: 100%; padding: 10px; background-color: #FF0000; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: 500; transition: background-color 0.2s; margin-top: 5px;}
        .auth-form button:hover, .upload-form button:hover, .friend-form button:hover, .reset-button:hover { background-color: #CC0000; }
        
        .logout-button { background-color: #555555 !important; }
        .logout-button:hover { background-color: #666666 !important; }

        /* Messages Flash */
        .flash { padding: 15px; margin-bottom: 20px; border-radius: 4px; font-weight: bold; }
        .success { background-color: #4CAF50; color: white; }
        .error { background-color: #FF5555; color: white; }
        .info { background-color: #3498db; color: white; }
        
        .section-title { color: #FFFFFF; font-size: 20px; font-weight: 500; margin-top: 30px; margin-bottom: 15px; }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">You<span>Tube</span> (Social Python)</div>
        {% if user_username %}
            <div class="user-action">
                <span class="material-icons" style="color: white; margin-right: 15px;">notifications</span>
                <span class="material-icons" style="color: white;">account_circle</span>
            </div>
        {% endif %}
    </div>

    <div class="main-layout">
        <div class="sidebar">
            <div class="sidebar-item" onclick="window.location.href='/'">
                <span class="material-icons">home</span> Accueil
            </div>
            <div class="sidebar-item">
                <span class="material-icons">explore</span> Explorer
            </div>
            <div class="sidebar-item">
                <span class="material-icons">subscriptions</span> Amis (Abonnements)
            </div>

            {% if user_username %}
                <h3>VOTRE COMPTE</h3>
                <p style="padding: 10px 15px; font-size: 14px;">Connecté: <br><strong>@{{ user_username }}</strong></p>
                
                <h3>ACTIONS RAPIDES</h3>
                <form class="friend-form" method="POST" action="{{ url_for('add_friend') }}" style="padding: 10px 0;">
                    <input type="text" name="friend_username" placeholder="Pseudo de l'ami" required>
                    <button type="submit" style="background-color: #2ECC71;">Ajouter Ami</button>
                </form>
                
                <form class="upload-form" method="POST" action="{{ url_for('upload_file') }}" enctype="multipart/form-data" style="padding: 10px 0;">
                    <input type="text" name="title" placeholder="Titre de la vidéo" required>
                    <input type="file" name="file" required>
                    <button type="submit">Uploader Vidéo</button>
                </form>

                <form method="POST" action="{{ url_for('logout') }}" style="margin-top: 20px; padding: 10px 0;">
                    <button type="submit" class="logout-button">Déconnexion</button>
                </form>

            {% else %}
                <h3>CONNEXION / INSCRIPTION</h3>
                <form class="auth-form" method="POST" action="{{ url_for('register') }}" style="padding: 10px 0;">
                    <input type="email" name="email" placeholder="Email" required>
                    <input type="text" name="username" placeholder="Pseudo" required>
                    <input type="password" name="password" placeholder="Mot de passe" required>
                    <button type="submit">S'inscrire</button>
                </form>
                <form class="auth-form" method="POST" action="{{ url_for('login') }}" style="padding: 10px 0;">
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

                <h2 class="section-title">En Tendances (Fil Vidéo Simulé)</h2>
                <div class="video-grid">
                    {% for video in uploaded_videos %}
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
                                        <a href="{{ url_for('download_file', filename=video.converted_filename) }}" download style="color: #FF0000; font-weight: 500;">Télécharger (Simulé)</a>
                                    {% endif %}
                                </div>
                            </div>
                        </div>
                    {% endfor %}
                    {% if not uploaded_videos %}
                        <p style="font-size: small; color: #AAAAAA;">Aucune vidéo publiée. Uploadez un fichier via le menu latéral.</p>
                    {% endif %}
                </div>
                
                <div class="chat-container">
                    <h2 class="section-title">💬 Messagerie Privée (Amis uniquement)</h2>
                    <p style="font-size: small; color: #AAAAAA; margin-bottom: 10px;">Liste des amis : {% for friend_name in friend_names %}@{{ friend_name }}{% if not loop.last %}, {% endif %}{% endfor %}</p>
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
                                    
                        // Afficher les messages système en rouge si l'utilisateur est 'Système'
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
# 4. FONCTIONS DE CONVERSION (SIMPLIFIÉES/DÉSACTIVÉES)
# --------------------------

def generate_unique_filename(extension):
    """Génère un nom de fichier unique."""
    return f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{random.randint(1000, 9999)}.{extension}"

def convert_to_mp4(input_path, output_dir):
    """Fonction de conversion DE-ACTIVÉE pour le déploiement sur Render."""
    print("ATTENTION: FFmpeg est désactivé. Retourne un fichier de test.")
    # Simuler la création d'un fichier de sortie
    simulated_filename = "simulated_video_" + generate_unique_filename("mp4")
    # Créer un fichier bidon pour simuler la conversion
    try:
        with open(os.path.join(output_dir, simulated_filename), 'w') as f:
            f.write("Ceci est un fichier vidéo simulé.")
    except Exception as e:
        print(f"Erreur lors de la création du fichier simulé: {e}")
        return None
        
    return simulated_filename

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
                # Récupère les noms des amis pour l'affichage dans l'interface
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
# 6. SOCKETIO (CHAT EN TEMPS RÉEL - LOGIQUE AMIS)
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
    Réceptionne le message, le stocke et l'émet UNIQUEMENT aux amis de l'expéditeur
    qui sont actuellement connectés.
    """
    user_username = session.get('user_username', 'Anonyme')
    text = data.get('text', '...')
    
    if text and user_username != 'Anonyme':
        with app.app_context():
            sender = User.query.filter_by(username=user_username).first()
            
            if not sender:
                # L'utilisateur de la session n'existe plus en DB
                return 

            message_data = {'user': user_username, 'text': text}
            chat_messages.append(message_data)
            
            # 1. Émettre le message à l'expéditeur lui-même (confirmation)
            emit('broadcast_message', message_data, room=request.sid)

            # 2. Émettre le message à chaque ami connecté
            friends_list = sender.friends.all()

            for friend in friends_list:
                friend_id = friend.id
                friend_sid = user_sid_map.get(friend_id)

                if friend_sid:
                    # Émet le message uniquement au socket de cet ami
                    emit('broadcast_message', message_data, room=friend_sid)
                    print(f"Message de @{user_username} envoyé à @{friend.username}.")
    else:
        # Émettre un message d'erreur à l'expéditeur
        error_data = {'user': 'Système', 'text': 'Veuillez vous connecter pour parler.'}
        emit('broadcast_message', error_data, room=request.sid)


# --------------------------
# 7. LANCEMENT 
# --------------------------

if __name__ == '__main__':
    PORT_CHOISI = 5003 
    socketio.run(app, debug=True, port=PORT_CHOISI)