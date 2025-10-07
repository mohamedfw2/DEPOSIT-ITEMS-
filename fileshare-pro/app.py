import os
import hashlib
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, url_for, send_file, flash, session
from werkzeug.utils import secure_filename
import zipfile
import io

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'changez-cette-cle-en-production')

# Configuration PostgreSQL
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Configuration
UPLOAD_FOLDER = '/opt/render/project/src/uploads'
MAX_FILE_SIZE = int(os.environ.get('MAX_FILE_SIZE_MB', 100)) * 1024 * 1024
MAX_FILES_PER_USER = int(os.environ.get('MAX_FILES_PER_USER', 10))  # Max 10 fichiers par utilisateur

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        print(f"Erreur de connexion DB: {e}")
        return None

def init_database():
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            # Table utilisateurs
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(255) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Table fichiers (plusieurs fichiers par utilisateur)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS files (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    filename VARCHAR(255) NOT NULL,
                    original_filename VARCHAR(255) NOT NULL,
                    file_size BIGINT NOT NULL,
                    upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    download_count INTEGER DEFAULT 0
                )
            ''')
            conn.commit()
            cursor.close()
            conn.close()
            print("✅ Base de données initialisée")
        except Exception as e:
            print(f"❌ Erreur init DB: {e}")

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def create_or_get_user(username, password):
    """Créer un utilisateur ou retourner l'ID si existe"""
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            # Vérifier si l'utilisateur existe
            cursor.execute('SELECT id, password_hash FROM users WHERE username = %s', (username,))
            user = cursor.fetchone()
            
            if user:
                # Vérifier le mot de passe
                if user['password_hash'] == hash_password(password):
                    cursor.close()
                    conn.close()
                    return user['id']
                else:
                    cursor.close()
                    conn.close()
                    return None  # Mauvais mot de passe
            else:
                # Créer nouvel utilisateur
                cursor.execute('''
                    INSERT INTO users (username, password_hash)
                    VALUES (%s, %s) RETURNING id
                ''', (username, hash_password(password)))
                user_id = cursor.fetchone()['id']
                conn.commit()
                cursor.close()
                conn.close()
                return user_id
        except Exception as e:
            print(f"❌ Erreur create_user: {e}")
            return None
    return None

def save_file_data(user_id, filename, original_filename, file_size):
    """Sauvegarder un fichier pour un utilisateur"""
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO files (user_id, filename, original_filename, file_size)
                VALUES (%s, %s, %s, %s)
            ''', (user_id, filename, original_filename, file_size))
            conn.commit()
            cursor.close()
            conn.close()
            return True
        except Exception as e:
            print(f"❌ Erreur sauvegarde fichier: {e}")
            return False
    return False

def get_user_files(username, password):
    """Récupérer tous les fichiers d'un utilisateur"""
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT f.*, u.username 
                FROM files f
                JOIN users u ON f.user_id = u.id
                WHERE u.username = %s AND u.password_hash = %s
                ORDER BY f.upload_date DESC
            ''', (username, hash_password(password)))
            files = cursor.fetchall()
            cursor.close()
            conn.close()
            return files
        except Exception as e:
            print(f"❌ Erreur récupération fichiers: {e}")
            return []
    return []

def get_user_stats(username):
    """Statistiques utilisateur"""
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 
                    COUNT(*) as total_files,
                    SUM(file_size) as total_size,
                    SUM(download_count) as total_downloads
                FROM files f
                JOIN users u ON f.user_id = u.id
                WHERE u.username = %s
            ''', (username,))
            stats = cursor.fetchone()
            cursor.close()
            conn.close()
            return stats
        except Exception as e:
            print(f"❌ Erreur stats: {e}")
            return None
    return None

def increment_download_count(file_id):
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute('UPDATE files SET download_count = download_count + 1 WHERE id = %s', (file_id,))
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"❌ Erreur increment: {e}")

def delete_user_files(user_id):
    """Supprimer les anciens fichiers d'un utilisateur"""
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            # Récupérer les noms de fichiers à supprimer
            cursor.execute('SELECT filename FROM files WHERE user_id = %s', (user_id,))
            files = cursor.fetchall()
            
            # Supprimer les fichiers physiques
            for file in files:
                filepath = os.path.join(UPLOAD_FOLDER, file['filename'])
                if os.path.exists(filepath):
                    os.remove(filepath)
            
            # Supprimer de la base
            cursor.execute('DELETE FROM files WHERE user_id = %s', (user_id,))
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"❌ Erreur suppression: {e}")

def format_file_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
    size_names = ["B", "KB", "MB", "GB"]
    i = 0
    while size_bytes >= 1024 and i < len(size_names)-1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.2f} {size_names[i]}"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        files = request.files.getlist('files')  # MULTIPLE FILES!
        replace_existing = request.form.get('replace_existing') == 'on'
        
        # Validations
        if not username or not password:
            flash('❌ Nom d\'utilisateur et mot de passe requis!')
            return redirect(request.url)
        
        if len(username) < 3:
            flash('❌ Le nom d\'utilisateur doit faire au moins 3 caractères!')
            return redirect(request.url)
            
        if len(password) < 4:
            flash('❌ Le mot de passe doit faire au moins 4 caractères!')
            return redirect(request.url)
        
        if not files or all(f.filename == '' for f in files):
            flash('❌ Aucun fichier sélectionné!')
            return redirect(request.url)
        
        # Limiter le nombre de fichiers
        valid_files = [f for f in files if f.filename != '']
        if len(valid_files) > MAX_FILES_PER_USER:
            flash(f'❌ Maximum {MAX_FILES_PER_USER} fichiers à la fois!')
            return redirect(request.url)
        
        # Créer ou récupérer l'utilisateur
        user_id = create_or_get_user(username, password)
        if not user_id:
            flash('❌ Nom d\'utilisateur déjà pris avec un autre mot de passe!')
            return redirect(request.url)
        
        # Si remplacement demandé, supprimer anciens fichiers
        if replace_existing:
            delete_user_files(user_id)
        
        # Upload des fichiers
        uploaded_count = 0
        total_size = 0
        
        for file in valid_files:
            # Vérifier la taille
            file.seek(0, os.SEEK_END)
            file_size = file.tell()
            file.seek(0)
            
            if file_size > MAX_FILE_SIZE:
                flash(f'⚠️ Fichier "{file.filename}" trop volumineux (max: {format_file_size(MAX_FILE_SIZE)})')
                continue
            
            # Sauvegarder le fichier
            original_filename = file.filename
            safe_filename = secure_filename(original_filename)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            unique_filename = f"{username}_{timestamp}_{safe_filename}"
            filepath = os.path.join(UPLOAD_FOLDER, unique_filename)
            
            try:
                file.save(filepath)
                if save_file_data(user_id, unique_filename, original_filename, file_size):
                    uploaded_count += 1
                    total_size += file_size
                else:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    flash(f'❌ Erreur pour "{original_filename}"')
            except Exception as e:
                flash(f'❌ Erreur pour "{original_filename}": {str(e)}')
        
        if uploaded_count > 0:
            flash(f'✅ {uploaded_count} fichier(s) uploadé(s) avec succès!')
            flash(f'📊 Taille totale: {format_file_size(total_size)}')
            flash(f'🔑 Identifiants: {username} / {password}')
            flash('📤 Partagez ces identifiants pour permettre le téléchargement!')
            return redirect(url_for('index'))
        else:
            flash('❌ Aucun fichier n\'a pu être uploadé!')
    
    return render_template('upload.html', max_files=MAX_FILES_PER_USER)

@app.route('/download', methods=['GET', 'POST'])
def download():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        if not username or not password:
            flash('❌ Nom d\'utilisateur et mot de passe requis!')
            return redirect(request.url)
        
        # Récupérer les fichiers
        files = get_user_files(username, password)
        
        if files:
            session['logged_in'] = True
            session['username'] = username
            session['password'] = password
            return redirect(url_for('files_list'))
        else:
            flash('❌ Identifiants incorrects ou aucun fichier trouvé!')
    
    return render_template('download.html')

@app.route('/files')
def files_list():
    if not session.get('logged_in'):
        flash('❌ Vous devez vous connecter d\'abord!')
        return redirect(url_for('download'))
    
    username = session.get('username')
    password = session.get('password')
    
    files = get_user_files(username, password)
    stats = get_user_stats(username)
    
    return render_template('files_list.html', files=files, stats=stats, format_size=format_file_size)

@app.route('/download_file/<int:file_id>')
def download_single_file(file_id):
    if not session.get('logged_in'):
        flash('❌ Vous devez vous connecter d\'abord!')
        return redirect(url_for('download'))
    
    username = session.get('username')
    password = session.get('password')
    
    # Vérifier que le fichier appartient à l'utilisateur
    files = get_user_files(username, password)
    file_data = next((f for f in files if f['id'] == file_id), None)
    
    if file_data:
        filepath = os.path.join(UPLOAD_FOLDER, file_data['filename'])
        if os.path.exists(filepath):
            increment_download_count(file_id)
            return send_file(filepath, as_attachment=True, download_name=file_data['original_filename'])
    
    flash('❌ Fichier non trouvé!')
    return redirect(url_for('files_list'))

@app.route('/download_all')
def download_all():
    if not session.get('logged_in'):
        flash('❌ Vous devez vous connecter d\'abord!')
        return redirect(url_for('download'))
    
    username = session.get('username')
    password = session.get('password')
    
    files = get_user_files(username, password)
    
    if not files:
        flash('❌ Aucun fichier à télécharger!')
        return redirect(url_for('files_list'))
    
    # Créer un ZIP en mémoire
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file_data in files:
            filepath = os.path.join(UPLOAD_FOLDER, file_data['filename'])
            if os.path.exists(filepath):
                zipf.write(filepath, file_data['original_filename'])
                increment_download_count(file_data['id'])
    
    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'{username}_files.zip'
    )

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

init_database()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
