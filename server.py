import os
import time
import json
import hashlib
import uuid
import requests # NUEVO: Para enviar mensajes a Telegram
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder='.', static_url_path='')

DB_URI = os.getenv("DATABASE_URL")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "supersecreto123")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8905492002:AAHGxqlBtlTXRcso66at_cMjShQECGQpbwA") # NUEVO

# Función helper para Telegram (Soporta Imágenes)
def send_telegram_msg(chat_id, text, image_url=None):
    if not TELEGRAM_BOT_TOKEN or not chat_id: return
    try:
        if image_url:
            # Si hay imagen, usa el endpoint sendPhoto
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            payload = {"chat_id": chat_id, "photo": image_url, "caption": text}
        else:
            # Si no hay imagen, envía solo texto
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": chat_id, "text": text}
            
        requests.post(url, json=payload, timeout=3)
    except Exception as e:
        print("Telegram error:", e)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(50) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            balance NUMERIC(12, 2) DEFAULT 0.00,
            phone_nequi VARCHAR(15),
            game_state TEXT,
            session_token VARCHAR(120),
            telegram_chat_id VARCHAR(50)
        );
    ''')
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS session_token VARCHAR(120);")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat_id VARCHAR(50);") # NUEVO
    # ... (Resto de la función init_db intacta)
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tournaments (
            id SERIAL PRIMARY KEY,
            title VARCHAR(100) NOT NULL,
            entry_fee_cop NUMERIC(10, 2) NOT NULL,
            rake_percentage NUMERIC(5, 2) DEFAULT 15.00,
            prize_pool_cop NUMERIC(12, 2) DEFAULT 0.00,
            status VARCHAR(20) DEFAULT 'open'
        );
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tournament_entries (
            id SERIAL PRIMARY KEY,
            tournament_id INTEGER REFERENCES tournaments(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            final_score NUMERIC(10, 2) DEFAULT 0.00,
            reward_cop NUMERIC(10, 2) DEFAULT 0.00,
            CONSTRAINT unique_entry_per_tournament UNIQUE(tournament_id, user_id)
        );
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS financial_ledger (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
            transaction_type VARCHAR(30) NOT NULL,
            amount_cop NUMERIC(12, 2) NOT NULL,
            external_reference VARCHAR(120) UNIQUE NOT NULL,
            status VARCHAR(20) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
    ''')
    
    # NUEVA ESTRUCTURA PARA SALAS DE TORNEO SIT & GO (10 JUGADORES)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tournament_instances (
            id SERIAL PRIMARY KEY,
            template_id INTEGER NOT NULL,
            entry_fee_cop NUMERIC(10, 2) NOT NULL,
            status VARCHAR(20) DEFAULT 'waiting',
            players_count INTEGER DEFAULT 0,
            max_players INTEGER DEFAULT 10,
            prize_pool_cop NUMERIC(12, 2) DEFAULT 0.00,
            start_time BIGINT DEFAULT 0,
            end_time BIGINT DEFAULT 0
        );
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tournament_players (
            id SERIAL PRIMARY KEY,
            instance_id INTEGER REFERENCES tournament_instances(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            username VARCHAR(50) NOT NULL,
            current_score NUMERIC(10, 2) DEFAULT 10000.00,
            CONSTRAINT unique_player_instance UNIQUE(instance_id, user_id)
        );
    ''')
    # Añadir las columnas dinámicamente si no existen (para la precisión y el tiempo)
    cur.execute("ALTER TABLE tournament_players ADD COLUMN IF NOT EXISTS alchemy_precision NUMERIC(6,3) DEFAULT 0.000;")
    cur.execute("ALTER TABLE tournament_players ADD COLUMN IF NOT EXISTS alchemy_time NUMERIC(5,2) DEFAULT 0.00;")
    conn.commit()
    cur.close()
    conn.close()


def get_db_connection():
    # Sistema de reintento para evitar que el servidor se caiga por el límite estricto de Aiven
    for _ in range(6):
        try:
            return psycopg2.connect(DB_URI, cursor_factory=RealDictCursor)
        except psycopg2.OperationalError:
            time.sleep(0.5)
    # Último intento
    return psycopg2.connect(DB_URI, cursor_factory=RealDictCursor)


init_db()

# ==========================================================
# MOTOR AUTORITATIVO DEL SERVIDOR (SERVER-SIDE VALIDATION)
# ==========================================================

def get_season_multiplier(now_ms):
    dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    day = dt.day
    if 1 <= day <= 9: return 0.5   
    elif 10 <= day <= 19: return 1.5 
    elif 20 <= day <= 26: return 1.0 
    else: return 2.0                 

def process_server_tick(game_state_str, chat_id=None):
    import random
    
    if not game_state_str: return game_state_str
    try:
        state = json.loads(game_state_str)
        now = int(time.time() * 1000)
        last_tick = state.get('lastTick', now)
        delta_ms = now - last_tick
        
        if delta_ms > 0:
            delta_hours = delta_ms / 3600000.0
            auto_water_end = state.get('autoWaterEndTime', 0)
            is_auto_watering = auto_water_end > now
            
            auto_harvest_end = state.get('autoHarvestEndTime', 0)
            is_auto_harvesting = auto_harvest_end > now
            
            season_multiplier = get_season_multiplier(now)

            any_plant_infected = any(
                p.get('status') == 'planted' and p.get('hasCrow') 
                for p in state.get('plots', [])
            )

            # NUEVO: Bandera para procesar el contagio del Cuervo Carroñero
            contagion_triggered = False

            for plot in state.get('plots', []):
                # Inicialización de banderas para evitar spam de notificaciones en Telegram
                if 'notifyFlags' not in plot: 
                    plot['notifyFlags'] = {'crow': False, 'ready': False, 'water': False, 'spoiled': False}
                flags = plot['notifyFlags']

                # Omitir daño a la planta del torneo si aún está "en espera" (waiting)
                if plot.get('status') in ['tournament_waiting']:
                    continue

                if plot.get('status') == 'tournament':
                    if any_plant_infected and not plot.get('hasCrow') and plot.get('crowLeavingAt', 0) <= now:
                        plot['hasCrow'] = True
                    
                    if plot.get('hasCrow') and plot.get('crowLeavingAt', 0) > 0 and now >= plot.get('crowLeavingAt'):
                        plot['hasCrow'] = False
                        plot['crowLeavingAt'] = 0

                elif plot.get('status') in ['empty', 'pot']:
                    if plot.get('water', 0) > 0:
                        accelerated_evap = 40.0 * 4.0 * season_multiplier # 4 veces más rápido sin planta
                        plot['water'] = max(0, plot['water'] - (accelerated_evap * delta_hours))

                elif plot.get('status') == 'planted':
                    plant_id = plot.get('plant', {}).get('id') if plot.get('plant') else None
                    
                    min_safe = 20
                    max_safe = 100
                    if plant_id == 'flower_wheat': min_safe, max_safe = 60, 100
                    elif plant_id == 'flower_bamboo': min_safe, max_safe = 50, 90 # NUEVO
                    elif plant_id == 'flower_small': min_safe, max_safe = 30, 90
                    elif plant_id == 'flower_big': min_safe, max_safe = 50, 70
                    elif plant_id == 'flower_cactus': min_safe, max_safe = 20, 80
                    elif plant_id == 'flower_crystal': min_safe, max_safe = 40, 80
                    elif plant_id == 'flower_moon': min_safe, max_safe = 40, 90
                    elif plant_id == 'flower_solar': min_safe, max_safe = 30, 70
                    elif plant_id == 'flower_neon': min_safe, max_safe = 40, 60

                    if not plot.get('isReady'):
                        if is_auto_watering:
                            plot['water'] = max_safe 
                            plot['hasCrow'] = False
                        else:
                            # -------- AQUÍ VA TU BLOQUE EXACTO --------
                            evap_rate = 800.0 if plant_id == 'flower_bamboo' else (10.0 if plant_id == 'flower_cactus' else 40.0)
                            evap_rate = evap_rate * season_multiplier 
                            # ------------------------------------------
                            
                            if plot.get('water', 0) > 0:
                                plot['water'] = max(0, plot['water'] - (evap_rate * delta_hours))
                            
                            if plot.get('hasCrow') and plot.get('crowLeavingAt', 0) > 0 and now >= plot.get('crowLeavingAt'):
                                plot['hasCrow'] = False
                                plot['crowLeavingAt'] = 0
                                if plot.get('water', 0) > max_safe:
                                    plot['water'] = max_safe

                        if plot.get('scarecrowEndTime', 0) > now:
                            plot['hasCrow'] = False
                        elif not plot.get('hasCrow') and not is_auto_watering:
                            if plot.get('water', 0) < min_safe:
                                plot['hasCrow'] = True
                            elif plot.get('water', 0) > max_safe:
                                plot['hasCrow'] = True
                            

                        if plot.get('hasCrow'):
                            crow_arrived = plot.get('crowArrivedAt', now)
                            plot['crowArrivedAt'] = crow_arrived
                            if plant_id == 'flower_wheat' and (now - crow_arrived) > 900000:
                                plot['status'] = 'empty'
                                plot['potUses'] = max(0, plot.get('potUses', 1) - 1)
                                plot['plant'] = None
                                plot['hasCrow'] = False
                                plot['crowArrivedAt'] = 0
                                plot['crowLeavingAt'] = 0
                        else:
                            plot['crowArrivedAt'] = 0

                        # Periodo de gracia de 10 minutos (600,000 ms) antes de pausar el crecimiento
                        time_with_crow = now - plot.get('crowArrivedAt', now)
                        is_crow_landed = plot.get('hasCrow') and time_with_crow > 600000
                        
                        if plot.get('water', 0) <= 0 or is_crow_landed:
                            plot['harvestAt'] = plot.get('harvestAt', now) + delta_ms

                    if now >= plot.get('harvestAt', now):
                        plot['isReady'] = True
                        
                        if is_auto_harvesting and not plot.get('isSpoiled'):
                            reward = 0
                            if plant_id == 'flower_wheat': reward = 1500
                            elif plant_id == 'flower_bamboo': reward = 6500 # NUEVO
                            elif plant_id == 'flower_cactus': reward = 3200
                            elif plant_id == 'flower_small': reward = 4500
                            elif plant_id == 'flower_big': reward = 26000
                            elif plant_id == 'flower_crystal': reward = 10000
                            elif plant_id == 'flower_moon': reward = 55000
                            elif plant_id == 'flower_solar': reward = 5000
                            elif plant_id == 'flower_neon': reward = 12000
                            
                            state['seedsBalance'] = state.get('seedsBalance', 0) + reward
                            plot['potUses'] = plot.get('potUses', 0) + 1
                            max_uses = 5 if plot.get('pot') == 'small' else 10
                            
                            if plot['potUses'] >= max_uses:
                                plot['status'] = 'empty'
                                plot['pot'] = None
                                plot['potUses'] = 0
                            else:
                                plot['status'] = 'pot'
                                
                            plot['plant'] = None
                            # El agua se conserva en la tierra para que se evapore al sol
                            plot['hasCrow'] = False
                            plot['crowLeavingAt'] = 0
                            plot['scarecrowEndTime'] = 0
                            plot['plantedAt'] = 0
                            plot['harvestAt'] = 0
                            plot['isReady'] = False
                            plot['isSpoiled'] = False
                        else:
                            # --- SISTEMA CUERVO CARROÑERO ---
                            growth_time = plot.get('harvestAt', now) - plot.get('plantedAt', now)
                            grace_period = growth_time / 2.0  # 50% del tiempo de vida
                            time_overdue = now - (plot.get('harvestAt', now) + grace_period)
                            
                            if time_overdue > 0:
                                plot['isSpoiled'] = True
                                plot['hasCrow'] = True
                                
                                # Tramos de 15 minutos = 900,000 ms
                                if time_overdue > 2700000: # 45 mins: 3 cuervos + Colapso
                                    plot['crowsCount'] = 3
                                    plot['rewardMultiplier'] = 0.0 # Pierde el 100% de la ganancia
                                    
                                    # Contagio al siguiente terreno (se procesa fuera del for)
                                    if not plot.get('contagionTriggered'):
                                        plot['contagionTriggered'] = True
                                        contagion_triggered = True
                                        
                                elif time_overdue > 1800000: # 30 mins: 3 cuervos (-5%/min extra)
                                    plot['crowsCount'] = 3
                                    mins_w_three = (time_overdue - 1800000) / 60000.0
                                    penalty = 0.30 + 0.45 + (mins_w_three * 0.05)
                                    plot['rewardMultiplier'] = max(0.0, 1.0 - penalty)
                                    
                                elif time_overdue > 900000: # 15 mins: 2 cuervos (-3%/min extra)
                                    plot['crowsCount'] = 2
                                    mins_w_two = (time_overdue - 900000) / 60000.0
                                    penalty = 0.30 + (mins_w_two * 0.03)
                                    plot['rewardMultiplier'] = max(0.0, 1.0 - penalty)
                                    
                                else: # 0 a 15 mins: 1 cuervo (-1%/min)
                                    plot['crowsCount'] = 1
                                    mins_w_one = time_overdue / 60000.0
                                    penalty = mins_w_one * 0.01
                                    plot['rewardMultiplier'] = max(0.0, 1.0 - penalty)
                    
                    # --- LÓGICA DE NOTIFICACIONES CARISMÁTICAS TELEGRAM ---
                    if chat_id and plot.get('status') == 'planted':
                        plant_name = plot.get('plant', {}).get('name', 'tu plantita')
                        plant_img = plot.get('plant', {}).get('icon', None)
                        p_id = plot['id'] + 1
                        
                        # MENSAJES SUAVES Y TIERNOS PARA CUERVO (Ataque)
                        crow_msgs = [
                            f"🦅 ¡Cua, cua! (Bueno, los cuervos no hacen así, pero entiendes la idea). ¡Un pajarraco me está picoteando en el Terreno {p_id}! 😭 ¡Ven a asustarlo antes de que me quede sin hojitas, por favor! 🥺",
                            f"🦅 ¡Auxilio, granjero/a! 🚨 Un cuervo gruñón aterrizó en la parcela {p_id} y me está mirando con cara de ensalada. ¡Sálvame, soy tu {plant_name} favorita! 🛡️🌱",
                            f"🦅 ¡Uy, qué miedo! 🫣 Hay un intruso con plumas en el Terreno {p_id} molestándome. Necesito a mi héroe (sí, ¡tú!) para que lo espante. 🥺"
                        ]
                        
                        # MENSAJES SUAVES PARA COSECHA LISTA (Felicidad)
                        ready_msgs = [
                            f"✨ ¡Tachán! 🎉 He crecido grande, fuerte y sanita en el Terreno {p_id}. Ya estoy lista para que me coseches y ganes tus semillitas. ¡Ven a verme, me veo genial! 🥰🌻",
                            f"✨ ¡Misión botánica cumplida! 🫡 Soy tu {plant_name} del Terreno {p_id} y ya llegué a mi mejor etapa. ¡Apúrate a recogerme para celebrar juntos! 🥳🌱",
                            f"✨ ¡Brillo más que el sol! 😍 Estoy lista en la parcela {p_id} y llena de recompensas para ti. ¡Ven rapidito a cosecharme, que huelo delicioso! 🧺💖"
                        ]
                        
                        # MENSAJES SUAVES PARA SEQUÍA (Empatía)
                        water_msgs = [
                            f"💧 ¡Tengo mucha sed! 🥺 Estoy en el Terreno {p_id} soñando con un chapuzón. ¿Me regalarías un poquito de agua de tu regadera mágica? 🚿🪴",
                            f"💧 ¡Ay, qué calorcito hace por aquí! 🥵 Tu {plant_name} en la parcela {p_id} se está secando un poquito. ¡Ven a refrescarme para seguir creciendo feliz! 🏜️🌱",
                            f"💧 Glu, glu... oh, espera, ¡no hay agüita! 😢 Mis raíces en el Terreno {p_id} están buscando humedad. ¡Un chorrito me haría la planta más feliz del mundo! 🚰💖"
                        ]
                        
                        # MENSAJES SUAVES PARA PUDRICIÓN (Tristeza tierna)
                        spoiled_msgs = [
                            f"🦠 Sniff, sniff... me he enfermado en el Terreno {p_id}. 🤧 Tengo un hongo molesto porque hubo un problemita con el riego. Ven a limpiarme, ¡te prometo ser más fuerte la próxima vez! 🥺🩹",
                            f"🦠 ¡Achoo! 🤒 Ups, creo que me pasé de humedad en la parcela {p_id} y ahora estoy marchita. ¿Me ayudarías con el espantapájaros? Quiero volver a sonreír. 🪴✨",
                            f"🦠 Oh no, me siento pachuchita. 🥀 Estoy un poco enfermita en el Terreno {p_id}. Tomará 15 minutitos limpiarme, pero te juro que valdrá la pena. ¡No me dejes así, porfis! 🗑️💔"
                        ]
                        
                        if plot.get('hasCrow') and not flags.get('crow'):
                            send_telegram_msg(chat_id, random.choice(crow_msgs), plant_img)
                            flags['crow'] = True
                        elif plot.get('isReady') and not flags.get('ready') and not is_auto_harvesting:
                            send_telegram_msg(chat_id, random.choice(ready_msgs), plant_img)
                            flags['ready'] = True
                        elif plot.get('water', 100) < min_safe and not plot.get('isReady') and not plot.get('hasCrow') and not flags.get('water'):
                            send_telegram_msg(chat_id, random.choice(water_msgs), plant_img)
                            flags['water'] = True
                        elif plot.get('isSpoiled') and not flags.get('spoiled'):
                            send_telegram_msg(chat_id, random.choice(spoiled_msgs), plant_img)
                            flags['spoiled'] = True

                        if not plot.get('hasCrow'): flags['crow'] = False
                        if not plot.get('isReady'): flags['ready'] = False
                        if plot.get('water', 100) >= min_safe: flags['water'] = False
                        if not plot.get('isSpoiled'): flags['spoiled'] = False

            # --- PROCESAR CONTAGIO DEL ÉXODO ---
            # Si una planta colapsó (pasaron 45 min extra), contagia a la siguiente parcela viva.
            if contagion_triggered:
                for p in state.get('plots', []):
                    if p.get('status') == 'planted' and not p.get('hasCrow') and not p.get('isSpoiled'):
                        p['hasCrow'] = True
                        p['crowArrivedAt'] = now
                        break # Solo propaga a 1 planta sana a la vez

            state['lastTick'] = now
            return json.dumps(state)
    except Exception as e:
        print("Server tick error:", e)
    return game_state_str

@app.route('/')
def serve_index(): return send_from_directory('.', 'index.html')
@app.route('/views/<path:path>')
def serve_views(path): return send_from_directory('views', path)

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    if cur.fetchone(): return jsonify({"error": "El usuario ya existe"}), 400
    
    hashed_pw = generate_password_hash(password)
    token = str(uuid.uuid4())
    
    cur.execute("INSERT INTO users (username, password_hash, balance, session_token) VALUES (%s, %s, 0, %s) RETURNING id, username, balance, session_token", (username, hashed_pw, token))
    new_user = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    return jsonify({"message": "Registro exitoso", "user": new_user})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = %s", (data.get('username'),))
    user = cur.fetchone()
    if user and check_password_hash(user['password_hash'], data.get('password')):
        token = str(uuid.uuid4())
        cur.execute("UPDATE users SET session_token = %s WHERE id = %s", (token, user['id']))
        conn.commit()
        
        del user['password_hash']
        user['balance'] = float(user['balance'] or 0)
        user['session_token'] = token
        
        cur.close(); conn.close()
        return jsonify({"message": "Login exitoso", "user": user})
    
    cur.close(); conn.close()
    return jsonify({"error": "Usuario o contraseña incorrectos"}), 401

@app.route('/api/sync/<username>', methods=['GET'])
def sync_user(username):
    token = request.args.get('token')
    conn = get_db_connection()
    cur = conn.cursor()
    # MODIFICADO: Extraemos el telegram_chat_id para enviarlo en el objeto 'user'
    cur.execute("SELECT balance, game_state, session_token, telegram_chat_id FROM users WHERE username = %s", (username,))
    user_data = cur.fetchone()
    if user_data:
        if user_data.get('session_token') and user_data['session_token'] != token:
            cur.close(); conn.close()
            return jsonify({"error": "Sesión expirada"}), 401
            
        validated_state = process_server_tick(user_data['game_state'], user_data.get('telegram_chat_id'))
        if validated_state != user_data['game_state']:
            cur.execute("UPDATE users SET game_state = %s WHERE username = %s", (validated_state, username))
            conn.commit()
            user_data['game_state'] = validated_state

        user_data['balance'] = float(user_data['balance'] or 0)
        cur.close(); conn.close()
        return jsonify({"status": "success", "user": user_data}), 200
    
    cur.close(); conn.close()
    return jsonify({"error": "Usuario no encontrado"}), 404

@app.route('/api/save_state', methods=['POST'])
def save_state():
    data = request.json
    username = data.get('username')
    token = data.get('session_token')
    incoming_state_str = data.get('game_state')
    
    conn = get_db_connection()
    cur = conn.cursor()
    # MODIFICADO: Incluir telegram_chat_id en la selección FOR UPDATE
    cur.execute("SELECT id, game_state, session_token, telegram_chat_id FROM users WHERE username = %s FOR UPDATE", (username,))
    user = cur.fetchone()
    
    if user:
        if user.get('session_token') and user['session_token'] != token:
            cur.close(); conn.close()
            return jsonify({"error": "Múltiples sesiones detectadas"}), 401
            
        try:
            incoming_state = json.loads(incoming_state_str)
            db_state_raw = user['game_state']
            db_state = json.loads(db_state_raw) if db_state_raw else {}
            
            incoming_seeds = incoming_state.get('seedsBalance', 0)
            
            db_seeds = db_state.get('seedsBalance', 35000 if not db_state_raw else 0)
            
            # MODIFICADO: Aumentamos el límite de 30000 a 80000 para permitir la cosecha del Lirio Lunar (55k)
            if incoming_seeds > db_seeds + 80000:
                incoming_state['seedsBalance'] = db_seeds
                
            plots = incoming_state.get('plots', [])
            if len(plots) > 8 and plots[8].get('status') in ['tournament', 'tournament_waiting']:
                t_score = plots[8].get('score', 10000)
                t_instance = plots[8].get('instanceId')
                if t_instance:
                    cur.execute("UPDATE tournament_players SET current_score = %s WHERE user_id = %s AND instance_id = %s", (t_score, user['id'], t_instance))
                
            incoming_state_str = json.dumps(incoming_state)
            # MODIFICADO: Pasar el chat_id a la validación
            validated_state_str = process_server_tick(incoming_state_str, user.get('telegram_chat_id'))
        except Exception:
            validated_state_str = process_server_tick(incoming_state_str, user.get('telegram_chat_id'))
            
        cur.execute("UPDATE users SET game_state = %s WHERE username = %s", (validated_state_str, username))
        conn.commit()
    
    cur.close(); conn.close()
    return jsonify({"message": "Progreso guardado y validado"})


@app.route('/api/telegram/link', methods=['POST'])
def link_telegram():
    data = request.json
    username = data.get('username')
    token = data.get('session_token')
    chat_id = data.get('telegram_chat_id')
    
    if not chat_id or not chat_id.replace('-', '').isdigit():
        return jsonify({"error": "Debes ingresar tu ID Numérico (Ej: 123456789), no tu @usuario. Usa @userinfobot en Telegram."}), 400
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET telegram_chat_id = %s WHERE username = %s AND session_token = %s RETURNING id", (chat_id, username, token))
    user_updated = cur.fetchone()
    
    if user_updated:
        conn.commit()
        cur.close()
        conn.close()
        # MENSAJE DE BIENVENIDA ACTUALIZADO
        send_telegram_msg(chat_id, "🌱 ¡Yupi, conexión exitosa! Soy el asistente de CashGarden. Prometo cuidar tus plantitas desde aquí y avisarte rápidamente si necesitan mimos o ayuda. 👩‍🌾✨")
        return jsonify({"message": "Telegram vinculado con éxito."})
        
    cur.close()
    conn.close()
    return jsonify({"error": "Sesión no válida o no autorizada"}), 401

@app.route('/api/cron/check_plants', methods=['GET'])
def cron_check_plants():
    # Este endpoint simula el paso del tiempo para usuarios offline y dispara las alertas
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, username, game_state, telegram_chat_id FROM users WHERE telegram_chat_id IS NOT NULL")
    users = cur.fetchall()
    
    for u in users:
        new_state = process_server_tick(u['game_state'], u['telegram_chat_id'])
        if new_state != u['game_state']:
            cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (new_state, u['id']))
            
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": "Cron procesado. Alertas enviadas si era necesario."})


@app.route('/api/store/buy_practice', methods=['POST'])
def buy_practice():
    data = request.json
    username = data.get('username')
    token = data.get('session_token')
    cost = int(data.get('cost', 500))
    amount = int(data.get('amount', 5))
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, game_state, session_token FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cur.fetchone()
        
        if not user: return jsonify({"error": "Usuario no encontrado"}), 404
        if user.get('session_token') and user['session_token'] != token:
            cur.close(); conn.close()
            return jsonify({"error": "Sesión inválida"}), 401
            
        state = json.loads(user['game_state'] or '{}')
        seeds_balance = state.get('seedsBalance', 0)
        
        if seeds_balance < cost:
            return jsonify({"error": "No tienes Semillas (🌱) suficientes para comprar el pase."}), 400
            
        state['seedsBalance'] = seeds_balance - cost
        state['practiceTokens'] = state.get('practiceTokens', 0) + amount
        
        cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(state), user['id']))
        conn.commit()
        
        return jsonify({
            "message": "Prácticas adquiridas con éxito", 
            "new_seeds": state['seedsBalance'],
            "new_tokens": state['practiceTokens']
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()

@app.route('/api/store/buy_seeds', methods=['POST'])
def buy_seeds():
    data = request.json
    username = data.get('username')
    token = data.get('session_token')
    package = data.get('package_id')
    incoming_inventory = data.get('inventory') 
    
    costs = {
        "iap_water_24h": {"seeds": 0, "cop": 5000},
        "iap_water_72h": {"seeds": 0, "cop": 12000},
        "iap_harvest_24h": {"seeds": 0, "cop": 4000},
        "iap_harvest_72h": {"seeds": 0, "cop": 10000},
        "iap_combo_24h": {"seeds": 0, "cop": 7500},
        "iap_combo_72h": {"seeds": 0, "cop": 18000},
        "exc_10k": {"seeds": 4250, "cop": 10000},
        "exc_25k": {"seeds": 11000, "cop": 25000},
        "exc_50k": {"seeds": 22500, "cop": 50000}
    }
    
    standard_costs = {
        "tool_pot_small": 5000,
        "tool_pot_big": 15000,
        "tool_water": 500,
        "tool_scarecrow": 6000,
        "flower_wheat": 50,
        "flower_cactus": 800,
        "flower_small": 2000,
        "flower_big": 10000
    }
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, balance, game_state, session_token FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cur.fetchone()
        
        if not user: return jsonify({"error": "Usuario no encontrado"}), 404
        if user.get('session_token') and user['session_token'] != token:
            cur.close(); conn.close()
            return jsonify({"error": "Sesión inválida"}), 401
            
        state = json.loads(user['game_state'] or '{}')
        
        if package in costs:
            cost_cop = costs[package]['cop']
            seeds_amount = costs[package]['seeds']
            
            if float(user['balance']) < cost_cop:
                return jsonify({"error": "Saldo COP insuficiente. Completa ofertas o torneos para recargar."}), 400
                
            if seeds_amount > 0:
                state['seedsBalance'] = state.get('seedsBalance', 0) + seeds_amount
            
            cur.execute("UPDATE users SET balance = balance - %s, game_state = %s WHERE id = %s", (cost_cop, json.dumps(state), user['id']))
            cur.execute("INSERT INTO financial_ledger (user_id, transaction_type, amount_cop, external_reference, status) VALUES (%s, 'iap_purchase', %s, %s, 'completed')", (user['id'], cost_cop, f"IAP-{user['id']}-{int(time.time())}"))
            conn.commit()
            
            return jsonify({
                "message": "Transacción exitosa", 
                "new_balance_cop": float(user['balance']) - cost_cop, 
                "new_seeds": state.get('seedsBalance', 0)
            })
            
        elif package in standard_costs and incoming_inventory:
            qty = data.get('qty', 1)
            total_seed_cost = standard_costs[package] * qty
            
            if state.get('seedsBalance', 0) < total_seed_cost:
                return jsonify({"error": "Semillas insuficientes"}), 400
                
            state['seedsBalance'] -= total_seed_cost
            state['inventory'] = incoming_inventory 
            
            cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(state), user['id']))
            conn.commit()
            
            return jsonify({
                "message": "Compra exitosa",
                "new_seeds": state['seedsBalance']
            })
            
        else:
            return jsonify({"error": "Paquete inválido"}), 400
            
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()

# ==========================================================
# ENDPOINTS DE TORNEOS (SCHEDULED & SIT&GO HÍBRIDO)
# ==========================================================

def get_next_schedule_ms(t_id):
    now = datetime.now(timezone.utc)
    # Desfase para que no todos inicien a la vez: Bonsái(0h), Orquídea(1h), Rosa(2h), Loto(3h)
    offset_hours = {1: 0, 3: 1, 4: 2, 5: 3}.get(t_id, 0)
    
    # Bloque base de 4 horas
    base_hour = ((now.hour // 4) + 1) * 4
    import datetime as dt
    next_time = now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(hours=base_hour + offset_hours)
    
    # Si la hora calculada ya quedó atrás en el tiempo actual, saltar al siguiente ciclo
    if int(next_time.timestamp() * 1000) <= int(now.timestamp() * 1000):
        next_time += dt.timedelta(hours=4)
        
    return int(next_time.timestamp() * 1000)

def process_tournament_lifecycle(inst, conn, cur):
    now = int(time.time() * 1000)
    if not inst: return None
    
    t_id = inst.get('template_id')
    
    # MOTOR DE INICIO
    if inst['status'] == 'waiting' and now >= inst['start_time']:
        if t_id == 1 or inst['players_count'] >= 3:
            dur_hours = {1: 12, 3: 6, 4: 8, 5: 12}.get(t_id, 6)
            end_time = inst['start_time'] + (dur_hours * 3600000)
            cur.execute("UPDATE tournament_instances SET status = 'running', end_time = %s WHERE id = %s", (end_time, inst['id']))
            inst['status'] = 'running'
            inst['end_time'] = end_time
        else:
            # Reprogramar con el desfase correcto si no hay quorum
            new_start = get_next_schedule_ms(t_id)
            cur.execute("UPDATE tournament_instances SET start_time = %s WHERE id = %s", (new_start, inst['id']))
            inst['start_time'] = new_start
        conn.commit()
        
    # MOTOR DE CIERRE (Bloquea inscripciones, define ganador)
    if inst['status'] == 'running' and now >= inst['end_time']:
        cur.execute("UPDATE tournament_instances SET status = 'completed' WHERE id = %s", (inst['id'],))
        conn.commit()
        inst['status'] = 'completed'
        
    return inst

@app.route('/api/tournaments/join', methods=['POST'])
def join_tournament():
    data = request.json
    username = data.get('username')
    token = data.get('session_token')
    fee = float(data.get('fee', 0))
    t_id = int(data.get('tournament_id', 1))
    
    # Nuevas variables capturadas del frontend
    precision = float(data.get('precision', 0.000))
    time_used = float(data.get('time_used', 0.00))
    score = float(data.get('score', 10000.00)) # <-- NUEVO: Captura el puntaje inicial con descuento

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, balance, game_state, session_token FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cur.fetchone()
        if not user or (user.get('session_token') and user['session_token'] != token):
            return jsonify({"error": "Sesión inválida"}), 401

        # Buscar sala en espera, si no existe, crearla con horario programado
        cur.execute("SELECT id, players_count, max_players, start_time FROM tournament_instances WHERE template_id = %s AND status = 'waiting' LIMIT 1 FOR UPDATE", (t_id,))
        instance = cur.fetchone()
        
        if not instance:
            start_time = get_next_schedule_ms(t_id) # <-- Modificado para usar el ID
            cur.execute("INSERT INTO tournament_instances (template_id, entry_fee_cop, max_players, start_time, prize_pool_cop) VALUES (%s, %s, 1000, %s, %s) RETURNING id", (t_id, fee, start_time, 10000 if t_id == 1 else 0))
            instance_id = cur.fetchone()['id']
            players_count = 0
            tourney_status = 'waiting'
            end_time = 0
        else:
            instance_id = instance['id']
            players_count = instance['players_count']
            start_time = instance['start_time']
            tourney_status = 'waiting'
            end_time = 0

        seed_cost = {0: 5000, 5000: 5000, 10000: 15000, 30000: 30000, 50000: 50000}.get(fee, 5000)
        state = json.loads(user['game_state'] or '{}')
        if state.get('seedsBalance', 0) < seed_cost:
            return jsonify({"error": f"Requiere {seed_cost} 🌱 para participar"}), 400

        # Anti-Duplicado (Si cambia de torneo, debemos removerlo del anterior. Por seguridad, aquí limpiamos instancias pasadas en estado 'waiting')
        cur.execute("DELETE FROM tournament_players WHERE user_id = %s AND instance_id IN (SELECT id FROM tournament_instances WHERE status = 'waiting')", (user['id'],))

        # Cobro de Semillas y Dinero Real
        state['seedsBalance'] -= seed_cost
        if fee > 0:
            prize_addition = fee * 0.85 
            cur.execute("UPDATE tournament_instances SET prize_pool_cop = prize_pool_cop + %s WHERE id = %s", (prize_addition, instance_id))
            cur.execute("INSERT INTO financial_ledger (user_id, transaction_type, amount_cop, external_reference, status) VALUES (%s, 'tournament_entry', %s, %s, 'completed')", (user['id'], fee, f"WOMPI-{user['id']}-{int(time.time())}"))

        # NUEVO: Insertamos el current_score calculado en el frontend junto con la precisión
        cur.execute("INSERT INTO tournament_players (instance_id, user_id, username, alchemy_precision, alchemy_time, current_score) VALUES (%s, %s, %s, %s, %s, %s)", (instance_id, user['id'], username, precision, time_used, score))
        cur.execute("UPDATE tournament_instances SET players_count = players_count + 1 WHERE id = %s", (instance_id,))
        cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(state), user['id']))
        conn.commit()
        
        return jsonify({
            "message": "Inscripción exitosa",
            "instance_id": instance_id,
            "tourney_status": tourney_status,
            "start_time": start_time,
            "end_time": end_time,
            "new_balance_cop": float(user['balance']),
            "new_game_state": state
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close(); conn.close()


@app.route('/api/tournaments/global_leaderboard/<int:t_id>', methods=['GET'])
def global_leaderboard(t_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tournament_instances WHERE template_id = %s AND status IN ('waiting', 'running') ORDER BY id DESC LIMIT 1", (t_id,))
    inst = cur.fetchone()
    
    if inst:
        inst = process_tournament_lifecycle(inst, conn, cur)
        
    # Si se completó o no hay activos, buscamos al campeón histórico reciente
    if not inst or inst['status'] == 'completed':
        cur.execute("SELECT * FROM tournament_instances WHERE template_id = %s AND status = 'completed' ORDER BY id DESC LIMIT 1", (t_id,))
        last_inst = cur.fetchone()
        if last_inst:
            cur.execute('''SELECT username, current_score FROM tournament_players WHERE instance_id = %s ORDER BY current_score DESC, alchemy_precision DESC, alchemy_time ASC LIMIT 1''', (last_inst['id'],))
            winner = cur.fetchone()
            cur.close(); conn.close()
            return jsonify({
                "status": "completed",
                "winner": winner['username'] if winner else "Nadie",
                "winning_score": float(winner['current_score']) if winner else 0,
                "start_time": last_inst['start_time'],
                "end_time": last_inst['end_time'],
                "prize_pool": last_inst['prize_pool_cop']
            })
        cur.close(); conn.close()
        return jsonify({"status": "no_instance", "prize_pool": 10000 if t_id == 1 else 0})
        
    cur.execute('''
        SELECT username, current_score, alchemy_precision, alchemy_time 
        FROM tournament_players 
        WHERE instance_id = %s 
        ORDER BY current_score DESC, alchemy_precision DESC, alchemy_time ASC LIMIT 10
    ''', (inst['id'],))
    players = cur.fetchall()
    cur.close(); conn.close()
    
    return jsonify({
        "prize_pool": inst['prize_pool_cop'],
        "status": inst['status'],
        "start_time": inst['start_time'],
        "end_time": inst['end_time'],
        "players_count": inst['players_count'],
        "max_players": inst['max_players'],
        "leaderboard": players
    })

@app.route('/api/tournaments/status/<int:instance_id>', methods=['GET'])
def check_tournament_status(instance_id):
    conn = get_db_connection()
    cur = conn.cursor()
    # Ahora trae template_id para el motor
    cur.execute("SELECT template_id, status, start_time, end_time, players_count FROM tournament_instances WHERE id = %s", (instance_id,))
    inst = cur.fetchone()
    
    if inst:
        inst = process_tournament_lifecycle(inst, conn, cur)
        
    cur.close(); conn.close()
    return jsonify(inst if inst else {})

@app.route('/api/tournaments/leaderboard/<int:instance_id>', methods=['GET'])
def get_leaderboard(instance_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT prize_pool_cop, status, players_count, max_players FROM tournament_instances WHERE id = %s", (instance_id,))
    instance = cur.fetchone()
    
    cur.execute('''
        SELECT username, current_score, alchemy_precision, alchemy_time 
        FROM tournament_players 
        WHERE instance_id = %s 
        ORDER BY current_score DESC, alchemy_precision DESC, alchemy_time ASC LIMIT 10
    ''', (instance_id,))  # <-- CORRECCIÓN: Debe ser instance_id
    players = cur.fetchall()
    cur.close(); conn.close()
    
    return jsonify({
        "prize_pool": instance['prize_pool_cop'] if instance else 0,
        "status": instance['status'] if instance else 'unknown',
        "players_count": instance['players_count'] if instance else 0,
        "max_players": instance['max_players'] if instance else 10,
        "leaderboard": players
    })

@app.route('/api/wallet/payout', methods=['POST'])
def dispatch_nequi_payout():
    payload = request.get_json() or {}
    token = payload.get('session_token')
    amount_cop = float(payload.get('amount_cop', 0))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, balance, session_token FROM users WHERE username = %s FOR UPDATE", (payload.get('username'),))
    user = cur.fetchone()
    if not user or float(user['balance']) < amount_cop or amount_cop < 10000:
        cur.close(); conn.close()
        return jsonify({"error": "Fondos insuficientes o menores a $10.000"}), 400
    if user.get('session_token') and user['session_token'] != token:
        cur.close(); conn.close()
        return jsonify({"error": "Sesión inválida"}), 401
        
    cur.execute("UPDATE users SET phone_nequi = %s, balance = balance - %s WHERE id = %s", (payload.get('phone_nequi'), amount_cop, user['id']))
    payout_ref = f"PO-{user['id']}-{int(time.time())}"
    cur.execute("INSERT INTO financial_ledger (user_id, transaction_type, amount_cop, external_reference, status) VALUES (%s, 'payout', %s, %s, 'pending')", (user['id'], amount_cop, payout_ref))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"message": "Retiro tramitado hacia Nequi", "reference": payout_ref, "new_balance": float(user['balance']) - amount_cop}), 200

@app.route('/api/dev/add_cop', methods=['POST'])
def dev_add_cop():
    data = request.json
    if data.get('admin_token') != ADMIN_SECRET:
        return jsonify({"error": "No autorizado"}), 403
    
    username = data.get('username')
    amount = float(data.get('amount', 0))
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET balance = balance + %s WHERE username = %s RETURNING balance", (amount, username))
        updated_user = cur.fetchone()
        conn.commit()
        if updated_user:
            return jsonify({"message": "Saldo inyectado", "new_balance": float(updated_user['balance'])}), 200
        return jsonify({"error": "Usuario no encontrado"}), 404
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()

@app.route('/api/dev/reset_tournaments', methods=['POST'])
def dev_reset_tournaments():
    data = request.json
    if data.get('admin_token') != ADMIN_SECRET:
        return jsonify({"error": "No autorizado"}), 403
        
    username = data.get('username')
    clear_all = data.get('clear_all', False)
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        if user:
            cur.execute("DELETE FROM tournament_players WHERE user_id = %s", (user['id'],))
            if clear_all:
                cur.execute("UPDATE users SET balance = 0.00 WHERE id = %s", (user['id'],))
            conn.commit()
            return jsonify({"message": "Datos de desarrollo reseteados en BD"}), 200
        return jsonify({"error": "Usuario no encontrado"}), 404
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


import threading

def background_cron_worker():
    while True:
        time.sleep(60) # El servidor revisará las plantas en silencio cada 60 segundos
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            # Buscar a todos los usuarios que hayan vinculado su Telegram
            cur.execute("SELECT id, username, game_state, telegram_chat_id FROM users WHERE telegram_chat_id IS NOT NULL")
            users = cur.fetchall()
            
            for u in users:
                try:
                    # process_server_tick enviará el Telegram en tiempo real si hay una novedad
                    new_state = process_server_tick(u['game_state'], u['telegram_chat_id'])
                    if new_state != u['game_state']:
                        cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (new_state, u['id']))
                except Exception as e:
                    pass
                    
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            pass

# Iniciamos el motor automático justo antes de que arranque la app
threading.Thread(target=background_cron_worker, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)
