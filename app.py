from flask import Flask, request, jsonify, render_template, send_from_directory, session, redirect, url_for, Response
from flask_cors import CORS
from datetime import datetime, timedelta
import os
import requests
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import smtplib
import random
import string
from email.mime.text import MIMEText
load_dotenv()

def get_db_connection():
    conn = sqlite3.connect("shelfbuddy.db")
    conn.row_factory = sqlite3.Row
    return conn
def add_column_if_not_exists(cur, table, column, definition):
    cur.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cur.fetchall()]

    if column not in columns:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")



app = Flask(__name__)

def create_tables():
    conn = get_db_connection()
    cur = conn.cursor()

    # PRODUCTS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        category TEXT NOT NULL,
        shelf_life_room_closed INTEGER,
        shelf_life_room_opened INTEGER,
        shelf_life_refrigerated_closed INTEGER,
        shelf_life_refrigerated_opened INTEGER,
        shelf_life_frozen_closed INTEGER,
        shelf_life_frozen_opened INTEGER
    )
    """)

    # USERS
    cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    role TEXT DEFAULT 'user'
)
""")
    add_column_if_not_exists(cur, "users", "otp", "TEXT")
    add_column_if_not_exists(cur, "users", "otp_expiry", "TEXT")
    add_column_if_not_exists(cur, "users", "is_verified", "INTEGER DEFAULT 0")
    add_column_if_not_exists(cur, "users", "reset_token", "TEXT")
    add_column_if_not_exists(cur, "users", "reset_token_expiry", "TEXT")

    # PANTRY
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pantry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        product TEXT,
        expiry_date TEXT,
        UNIQUE(user_id, product, expiry_date),
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    # SUGGESTIONS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT,
        message TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()

app.secret_key = os.getenv("SECRET_KEY")
CORS(app)


def get_shelf_life(product, storage, opened):
    storage_map = {
        'room': 'room',
        'refrigerated': 'refrigerated',
        'frozen': 'frozen'
    }

    mapped_storage = storage_map.get(storage, 'room')
    column = f"shelf_life_{mapped_storage}_{'opened' if opened else 'closed'}"
    
    conn = get_db_connection()
    cur = conn.cursor()

    query = f"""
        SELECT {column}
        FROM products
        WHERE LOWER(name) LIKE ?
    """

    search_term = f"%{product.lower()}%"
    cur.execute(query, (search_term,))
    result = cur.fetchone()

    cur.close()
    conn.close()

    return result[0] if result and result[0] is not None else None

#Registration route

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form['email'].strip().lower()
        password = generate_password_hash(request.form['password'])

        conn = get_db_connection()
        cur = conn.cursor()

        try:
            # Insert user
            cur.execute(
                "INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
                (username, email, password)
            )

            # Generate OTP
            otp = str(random.randint(100000, 999999))
            expiry = (datetime.now() + timedelta(minutes=5)).isoformat()

            # Update OTP fields
            cur.execute("""
                UPDATE users
                SET otp=?, otp_expiry=?, is_verified=0
                WHERE email=?
            """, (otp, expiry, email))

            conn.commit()

        except sqlite3.IntegrityError:
            conn.rollback()
            cur.close()
            conn.close()
            flash("User already exists. Try logging in instead.", "error")
            return render_template("register.html")

        cur.close()
        conn.close()

        # Send email after DB commit
        send_email(
            email,
            "Verify Your Account - ShelfBuddy",
            f"Your OTP is {otp}. It expires in 5 minutes."
        )

        return redirect(url_for('verify_otp', email=email))

    return render_template("register.html")

#login route
from flask import flash

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, username, password, role, is_verified FROM users WHERE email=?", (email,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if not user:
            flash("User not found. Please register.", "error")
            return render_template("login.html")
        if user[4] == 0:
            flash("Please verify your email first.", "error")
            return render_template("login.html")
        from werkzeug.security import check_password_hash
        if not check_password_hash(user[2], password):
            flash("Incorrect password.", "error")
            return render_template("login.html")
    


        session['user_id'] = user[0]
        session['username'] = user[1]
        session['role'] = user[3]
        return redirect('/home')

    return render_template("login.html")

# Guest Mode route
@app.route('/guest')
def guest():
    session['guest'] = True
    session['user_id'] = None
    session['username'] = "Guest"
    return redirect(url_for('home'))


# Logout route
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('first_page'))

# Save to pantry route
@app.route('/save-to-pantry', methods=['POST'])
def save_to_pantry():

    if not session.get('user_id'):
        return jsonify({"status": "error", "message": "Login required."})

    data = request.json
    product = data.get("product")
    expiry_date = data.get("expiry_date")

    if not product or not expiry_date:
        return jsonify({"status": "error", "message": "Invalid data."})

    conn = get_db_connection()
    cur = conn.cursor()

    # Prevent duplicates
    cur.execute("""
        SELECT id FROM pantry
        WHERE user_id=? AND product=? AND expiry_date=?
    """, (session['user_id'], product, expiry_date))

    existing = cur.fetchone()

    if existing:
        cur.close()
        conn.close()
        return jsonify({"status": "error", "message": "Item already saved."})

    cur.execute("""
        INSERT INTO pantry (user_id, product, expiry_date)
        VALUES (?, ?, ?)
    """, (session['user_id'], product, expiry_date))

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"status": "success", "message": "Saved to pantry!"})

# Pantry route to display saved items and their expiry dates, sorted by nearest expiry first
@app.route('/pantry')
def pantry():

    if not session.get('user_id'):
        return redirect('/login')

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, product, expiry_date
        FROM pantry
        WHERE user_id=?
        ORDER BY expiry_date ASC
    """, (session['user_id'],))

    rows = cur.fetchall()
    cur.close()
    conn.commit()
    conn.close()

    pantry_items = []
    today = datetime.now().date()

    for row in rows:
        expiry = datetime.strptime(row["expiry_date"], "%Y-%m-%d").date()
        days_left = (expiry - today).days

        pantry_items.append({
            "id": row["id"],
            "product": row["product"],
            "expiry_date": row["expiry_date"],
            "days_left": days_left
        })

    return render_template("pantry.html", items=pantry_items)

# Route to delete an item from the pantry
@app.route('/delete-from-pantry', methods=['POST'])
def delete_from_pantry():

    if not session.get('user_id'):
        return jsonify({"status": "error", "message": "Unauthorized"})

    data = request.json
    item_id = data.get("item_id")

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        DELETE FROM pantry
        WHERE id=? AND user_id=?
    """, (item_id, session['user_id']))

    conn.commit()

    cur.close()
    conn.close()

    return jsonify({"status": "success"})


#pantry stats route to show number of expired and soon-to-expire items
@app.route('/pantry-stats')
def pantry_stats():
    if not session.get('user_id'):
        return jsonify({"expired":0,"soon":0,"safe":0,"total":0})

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT expiry_date FROM pantry
        WHERE user_id = ?
    """, (session['user_id'],))

    rows = cur.fetchall()
    cur.close()
    conn.close()

    expired = soon = safe = 0
    today = datetime.now().date()
    
    for (expiry_date,) in rows:
        expiry = datetime.strptime(expiry_date, "%Y-%m-%d").date()
        days = (expiry - today).days
        if days < 0:
            expired += 1
        elif days <= 3:
            soon += 1
        else:
            safe += 1

    return jsonify({
        "expired": expired,
        "soon": soon,
        "safe": safe,
        "total": expired + soon + safe
    })



# Route for robots.txt
@app.route('/robots.txt')
def serve_robots():
    return send_from_directory(
        os.path.join(app.root_path, 'static'),
        'robots.txt',
        mimetype='text/plain'
    )

# Route for sitemap.xml
@app.route('/sitemap.xml')
def sitemap_xml():
    return send_from_directory(app.static_folder, 'sitemap.xml')

@app.route('/get-product', methods=['POST'])
def get_product():
    data = request.json
    product = data.get('product', '').strip()
    storage = data.get('storage', 'room')
    opened = data.get('opened', False)
    manu_date = data.get('manufacturing_date')

    if not product:
        return jsonify({'status': 'error', 'message': 'Product name is required'}), 400

    shelf_life = get_shelf_life(product, storage, opened)
    if shelf_life is None:
        return jsonify({'status': 'error', 'message': 'Product not found or shelf life missing'}), 404

    if manu_date:
        try:
            if manu_date == "Invalid Date" or manu_date.strip() == "":
                raise ValueError
            mdate = datetime.strptime(manu_date, '%Y-%m-%d')
            expiry = mdate + timedelta(days=shelf_life)
            return jsonify({
                'status': 'success',
                'expiry_date': expiry.strftime('%Y-%m-%d'),
                'shelf_life': shelf_life
            })
        except:
            return jsonify({'status': 'error', 'message': 'Invalid date'}), 400

    return jsonify({
        'status': 'success',
        'shelf_life': shelf_life
    })


@app.route('/get-category-average', methods=['POST'])
def get_category_average():
    data = request.json
    category = data.get('category')

    if not category:
        return jsonify({'status': 'error', 'message': 'Category is required'}), 400

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT AVG(
            shelf_life_room_closed +
            shelf_life_refrigerated_closed +
            shelf_life_frozen_closed
        ) / 3
        FROM products
        WHERE LOWER(category) = ?
    """, (category.lower(),))

    avg = cur.fetchone()[0]

    cur.close()
    conn.close()

    if avg is None:
        return jsonify({'status': 'error', 'message': 'Category not found'}), 404

    return jsonify({
        'status': 'success',
        'category': category,
        'average_shelf_life': round(avg)
    })

@app.route('/')
def first_page():
    return render_template("landing.html")

@app.route('/home')
def home():
    return render_template("main.html")

# Debug route to check users in the database
# @app.route('/debug-users')
# def debug_users():
#     conn = get_db_connection()
#     cur = conn.cursor()
#     cur.execute("SELECT id, username, email FROM users")
#     users = cur.fetchall()
#     cur.close()
#     conn.close()
#     return str(users)

@app.route('/submit-suggestion', methods=['POST'])
def submit_suggestion():
    data = request.json
    name = data.get("name")
    email = data.get("email")
    message = data.get("message")

    if not message:
        return jsonify({"status": "error", "message": "Message required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        "INSERT INTO suggestions (name, email, message) VALUES (?, ?, ?)",
        (name, email, message)
    )

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"status": "success"})

@app.route('/suggest-recipe')
def suggest_recipe():
    return render_template("suggest_recipe.html")


@app.route('/get-recipes', methods=['POST'])
def get_recipes():

    data = request.json
    offset = data.get("offset", 0)

    ingredient1 = data.get("ingredient1")
    ingredient2 = data.get("ingredient2")
    ingredient3 = data.get("ingredient3")
    cuisine = data.get("cuisine")

    ingredients = ",".join(
        [i for i in [ingredient1, ingredient2, ingredient3] if i]
    )

    
    api_key = os.getenv("SPOONACULAR_API_KEY")

    url = "https://api.spoonacular.com/recipes/complexSearch"

    params = {
        "apiKey": api_key,
        "query": ingredient1,
        "number": 4,
        "offset": offset,
        "addRecipeInformation": True
    }

    # Only add these if they exist
    if ingredients:
        params["includeIngredients"] = ingredients

    if cuisine:
        params["cuisine"] = cuisine

    response = requests.get(url, params=params)

    return jsonify(response.json())

# @app.route('/debug-suggestions')
# def debug_suggestions():
#     conn = get_db_connection()
#     cur = conn.cursor()
#     cur.execute("SELECT * FROM suggestions")
#     rows = cur.fetchall()
#     conn.close()
#     return str(rows)

from functools import wraps

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):

        if not session.get("user_id"):
            return redirect("/login")

        if session.get("role") != "admin":
            return "Unauthorized", 403

        return f(*args, **kwargs)

    return decorated

@app.route("/admin")
@admin_required
def admin_dashboard():

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, username, email, role FROM users")
    users = cur.fetchall()

    cur.execute("SELECT id, name, category FROM products")
    products = cur.fetchall()

    cur.execute("SELECT id, name, email, message, created_at FROM suggestions ORDER BY id DESC")
    suggestions = cur.fetchall()

    conn.close()

    return render_template(
        "admin.html",
        users=users,
        products=products,
        suggestions=suggestions
    )

def send_email(to_email, subject, body):
    sender_email = os.getenv("EMAIL_USER")
    sender_password = os.getenv("EMAIL_PASS")

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = sender_email
    msg['To'] = to_email

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, to_email, msg.as_string())

@app.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    email = request.args.get('email')

    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        entered_otp = request.form['otp']

        user = cur.execute(
            "SELECT otp, otp_expiry FROM users WHERE email=?",
            (email,)
        ).fetchone()

        if not user:
            cur.close()
            conn.close()
            return "Invalid request"

        stored_otp, expiry = user

        if not expiry:
            cur.close()
            conn.close()
            return "OTP expired"

        expiry = datetime.fromisoformat(expiry)

        if entered_otp == stored_otp and datetime.now() < expiry:
            cur.execute(
                "UPDATE users SET is_verified=1, otp=NULL, otp_expiry=NULL WHERE email=?",
                (email,)
            )
            conn.commit()
            cur.close()
            conn.close()
            return redirect('/login')

        cur.close()
        conn.close()
        return "Invalid or Expired OTP"

    cur.close()
    conn.close()
    return render_template("verify_otp.html", email=email)


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    
    conn = get_db_connection()
    cur = conn.cursor()
    if request.method == 'POST':
        email = request.form['email']

        token = ''.join(random.choices(string.ascii_letters + string.digits, k=32))
        expiry = (datetime.now() + timedelta(minutes=15)).isoformat()
        cur.execute("SELECT id FROM users WHERE email=?", (email,))
        
        if not cur.fetchone():
            return "If account exists, reset link sent."
        cur.execute("""
        UPDATE users
        SET reset_token=?, reset_token_expiry=?
        WHERE email=?
        """, (token, expiry, email))

        conn.commit()
        cur.close()
        conn.close()
        reset_link = url_for('reset_password', token=token, _external=True)

        send_email(
            email,
            "Reset Your Password - ShelfBuddy",
            f"Click this link to reset your password:\n{reset_link}\nExpires in 15 minutes."
        )
        if not sender_email or not sender_password:
            raise Exception("Email credentials not configured")
        return "Password reset link sent!"

    return render_template("forgot_password.html")

@app.route('/reset/<token>', methods=['GET', 'POST'])
def reset_password(token):
    conn = get_db_connection()
    cur = conn.cursor()

    user = cur.execute("""
        SELECT email, reset_token_expiry
        FROM users
        WHERE reset_token=?
    """, (token,)).fetchone()

    if not user:
        cur.close()
        conn.close()
        return "Invalid Token"

    email, expiry = user

    if not expiry:
        cur.close()
        conn.close()
        return "Token expired"

    expiry = datetime.fromisoformat(expiry)

    if datetime.now() > expiry:
        cur.close()
        conn.close()
        return "Token Expired"

    if request.method == 'POST':
        new_password = request.form['password']
        hashed_password = generate_password_hash(new_password)

        cur.execute("""
            UPDATE users
            SET password=?, reset_token=NULL, reset_token_expiry=NULL
            WHERE email=?
        """, (hashed_password, email))

        conn.commit()
        cur.close()
        conn.close()
        return "Password Updated Successfully!"

    cur.close()
    conn.close()
    return render_template("reset_password.html")

if __name__ == '__main__':
    create_tables()
    port = int(os.environ.get("PORT", 10000))
    app.run(debug=True, host='0.0.0.0', port=port)
