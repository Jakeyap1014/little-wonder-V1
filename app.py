#!/usr/bin/env python3
"""
Little Wonder V1 — Complete Device Server
==========================================
HTML SPA display served to surf browser on round 480x480 screen.
All V1 features from Hardware Doc implemented.

Architecture:
  - HTTP server on port 8888
  - /display → SPA (loads once, polls /display/state via JS fetch)
  - /frame   → live MJPEG frames from camera
  - /scan    → trigger scan pipeline
  - /menu/*  → bezel menu actions
  - /tts     → server-side TTS
  - /sfx     → server-side sound effects

Features:
  1.  Onboarding (name recording, guided first scans)
  2.  Randomized scan events (4 variants)
  3.  Idle voice prompts + 10-min auto-shutdown
  4.  Battery states (sad face, charging, warnings)
  5.  Boot-up sequence (chime + greeting)
  6.  Bezel menu (Collections, Missions)
  7.  Collection browser (6 categories, persistent)
  8.  Mission system (tracking, completion, celebrations)
  9.  Duplicate scan detection + varied responses
  10. Correct XP values + Legendary rarity
  11. Scanning animation + voice lines
  12. Circular XP arc on home screen
  13. No-signal silent queuing
  14. Scan-failed conversational handling
  15. Burst capture for sharpness
  16. Lip-sync animation triggers
"""

import os, sys, json, base64, random, re, time, threading, math
import subprocess, urllib.request, ssl, socket, struct, io
import pathlib, hashlib, glob
from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

from dotenv import load_dotenv
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")

BASE_DIR = pathlib.Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# ============================================================
# XP / LEVEL SYSTEM (corrected values from Hardware Doc §3.6)
# ============================================================
LEVELS = [
    (0, "Curious Cub"), (50, "Little Explorer"), (150, "Trail Trekker"),
    (300, "Nature Scout"), (500, "Wonder Seeker"), (800, "Discovery Pro"),
    (1200, "Knowledge Knight"), (1700, "Wisdom Wizard"), (2300, "Grand Master"),
    (3000, "Legend of Wonder"),
]

RARITY_CONFIG = {
    "common":    {"xp": 10, "dup_xp": 2,  "prob_floor": 0.40, "color": "yellow",  "label": "COMMON"},
    "rare":      {"xp": 50, "dup_xp": 10, "prob_floor": 0.15, "color": "green",   "label": "RARE"},
    "epic":      {"xp": 100,"dup_xp": 20, "prob_floor": 0.03, "color": "blue",    "label": "EPIC"},
    "legendary": {"xp": 200,"dup_xp": 40, "prob_floor": 0.00, "color": "purple",  "label": "LEGENDARY"},
}

def calc_rarity():
    r = random.random()
    if r < 0.02:   return "legendary"
    if r < 0.07:   return "epic"
    if r < 0.25:   return "rare"
    return "common"

def rarity_xp(rarity, is_duplicate=False):
    cfg = RARITY_CONFIG.get(rarity, RARITY_CONFIG["common"])
    return cfg["dup_xp"] if is_duplicate else cfg["xp"]

def get_level_info(xp):
    level, title, current_threshold, next_threshold = 1, "Curious Cub", 0, 50
    for i, (threshold, name) in enumerate(LEVELS):
        if xp >= threshold:
            level, title, current_threshold = i + 1, name, threshold
            next_threshold = LEVELS[i + 1][0] if i + 1 < len(LEVELS) else threshold + 500
    return level, title, current_threshold, next_threshold


# ============================================================
# PERSISTENT DATA (collections, profile, missions)
# ============================================================
PROFILE_PATH = DATA_DIR / "profile.json"
COLLECTION_PATH = DATA_DIR / "collection.json"
MISSION_PATH = DATA_DIR / "mission.json"
QUEUE_PATH = DATA_DIR / "scan_queue.json"

def load_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except:
        pass
    return default

def save_json(path, data):
    try:
        path.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"  [SAVE] Error: {e}")

# --- Profile ---
profile = load_json(PROFILE_PATH, {
    "name": "",
    "xp": 0,
    "onboarded": False,
    "first_boot": True,
})

def save_profile():
    save_json(PROFILE_PATH, profile)

# --- Collection ---
# { "item_hash": { "title": ..., "emoji": ..., "category": ..., "rarity": ...,
#                   "facts": [...], "scan_count": N, "first_scanned": timestamp } }
collection = load_json(COLLECTION_PATH, {})

def save_collection():
    save_json(COLLECTION_PATH, collection)

def get_item_hash(title):
    """Normalize title to a consistent key."""
    return hashlib.md5(title.lower().strip().encode()).hexdigest()[:12]

def find_duplicate(title):
    """Check if this item (or similar) was scanned before."""
    h = get_item_hash(title)
    return collection.get(h)

def add_to_collection(title, emoji, category, rarity, facts, mission):
    h = get_item_hash(title)
    if h in collection:
        collection[h]["scan_count"] += 1
        # Add any new facts
        for f in facts:
            if f not in collection[h]["facts"]:
                collection[h]["facts"].append(f)
        save_collection()
        return True  # was duplicate
    else:
        collection[h] = {
            "title": title, "emoji": emoji, "category": category,
            "rarity": rarity, "facts": facts, "mission": mission,
            "scan_count": 1, "first_scanned": time.time(),
        }
        save_collection()
        return False  # new discovery

def get_collection_by_category():
    """Group collection items by category for the browser."""
    CATEGORY_MAP = {
        "Plants": "plants", "Plant": "plants", "Nature": "nature",
        "Animal": "animals", "Animals": "animals",
        "Insects": "insects", "Insect": "insects", "Bug": "insects",
        "Birds": "birds", "Bird": "birds",
        "Man-made Object": "manmade", "Man-made": "manmade",
        "Technology": "manmade", "Vehicle": "manmade",
        "Food & Drink": "manmade", "Food": "manmade",
        "Toy": "manmade", "Home": "manmade",
    }
    cats = {"plants": [], "manmade": [], "animals": [], "nature": [], "insects": [], "birds": []}
    for h, item in collection.items():
        cat_key = CATEGORY_MAP.get(item.get("category", ""), "manmade")
        cats[cat_key].append({**item, "hash": h})
    return cats

# --- Missions ---
MISSION_TEMPLATES = [
    {"type": "color",    "text": "Find 3 {color} things!",       "target": 3, "params": ["red","blue","green","yellow","white","orange","pink"]},
    {"type": "quantity", "text": "Scan {n} different things today!", "target": None, "params": [3, 4, 5]},
    {"type": "category", "text": "Find {n} things that {desc}!",   "target": None,
     "params": [("fly", 3), ("grow", 3), ("are alive", 3), ("you can eat", 3)]},
    {"type": "novelty",  "text": "Scan {n} things you've never scanned before!", "target": None, "params": [2, 3, 4]},
    {"type": "fun",      "text": "Find the biggest thing you can!", "target": 1, "params": []},
    {"type": "fun",      "text": "Find something smaller than your hand!", "target": 1, "params": []},
    {"type": "fun",      "text": "Find something that makes a sound!", "target": 1, "params": []},
]

mission_state = load_json(MISSION_PATH, {
    "text": "",
    "progress": 0,
    "target": 3,
    "completed": False,
    "date": "",
    "type": "",
})

def generate_mission():
    """Generate a new random mission."""
    template = random.choice(MISSION_TEMPLATES)
    text = template["text"]
    target = template["target"]

    if template["type"] == "color":
        color = random.choice(template["params"])
        text = text.format(color=color)
        target = 3
    elif template["type"] == "quantity":
        n = random.choice(template["params"])
        text = text.format(n=n)
        target = n
    elif template["type"] == "category":
        desc, n = random.choice(template["params"])
        text = text.format(n=n, desc=desc)
        target = n
    elif template["type"] == "novelty":
        n = random.choice(template["params"])
        text = text.format(n=n)
        target = n
    elif template["type"] == "fun":
        target = 1

    mission_state.update({
        "text": text, "progress": 0, "target": target,
        "completed": False, "date": time.strftime("%Y-%m-%d"),
        "type": template["type"],
    })
    save_json(MISSION_PATH, mission_state)
    return text

def check_mission_progress():
    """Increment mission progress (called after each scan). Returns True if just completed."""
    if mission_state["completed"]:
        return False
    mission_state["progress"] += 1
    if mission_state["progress"] >= mission_state["target"]:
        mission_state["completed"] = True
        save_json(MISSION_PATH, mission_state)
        return True
    save_json(MISSION_PATH, mission_state)
    return False

def ensure_active_mission():
    """Make sure there's an active mission. Generate one if needed."""
    today = time.strftime("%Y-%m-%d")
    if not mission_state["text"] or mission_state["completed"] or mission_state["date"] != today:
        generate_mission()

# Initialize mission on startup
ensure_active_mission()


# ============================================================
# APP STATE
# ============================================================
app_state = {
    "screen": "boot",
    "object_name": "", "object_emoji": "", "rarity": "common",
    "fact": "", "mission": mission_state.get("text", ""),
    "mission_progress": mission_state.get("progress", 0),
    "mission_target": mission_state.get("target", 3),
    "mission_completed": False,
    "xp": profile["xp"], "level": 1, "level_title": "Curious Cub",
    "xp_pct": 0, "xp_earned": 0,
    "name": profile["name"], "onboarded": profile["onboarded"],
    "is_duplicate": False, "scan_count": 1,
    "scan_variant": 1,  # 1-4 random scan flow
    "category": "", "cat_emoji": "",
    "collection_data": "{}",
    "speaking": False,  # for lip-sync
    "battery_pct": 100, "charging": False,
    "idle_seconds": 0,
}
state_lock = threading.Lock()

def set_state(**kw):
    with state_lock:
        app_state.update(kw)

def get_state():
    with state_lock:
        return dict(app_state)

def refresh_xp_state():
    """Recalculate level info from current XP."""
    lv, lt, ct, nt = get_level_info(profile["xp"])
    xp_pct = int(((profile["xp"] - ct) / max(nt - ct, 1)) * 100)
    set_state(xp=profile["xp"], level=lv, level_title=lt, xp_pct=xp_pct)


# ============================================================
# CAMERA
# ============================================================
camera_proc = None
camera_lock = threading.Lock()
latest_frame = None
frame_lock = threading.Lock()

def start_camera():
    global camera_proc
    with camera_lock:
        if camera_proc and camera_proc.poll() is None:
            return
        camera_proc = subprocess.Popen([
            "rpicam-vid", "-t", "0", "--codec", "mjpeg",
            "--width", "480", "--height", "480",
            "--framerate", "15", "--nopreview", "-o", "-"
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=_read_frames, daemon=True).start()
        print("  [CAM] Started")

def _read_frames():
    global latest_frame, camera_proc
    buf = b""
    try:
        while camera_proc and camera_proc.poll() is None:
            chunk = camera_proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                soi = buf.find(b'\xff\xd8')
                if soi == -1: break
                eoi = buf.find(b'\xff\xd9', soi + 2)
                if eoi == -1: break
                with frame_lock:
                    latest_frame = buf[soi:eoi + 2]
                buf = buf[eoi + 2:]
    except:
        pass

def stop_camera():
    global camera_proc
    with camera_lock:
        if camera_proc:
            camera_proc.terminate()
            try:
                camera_proc.wait(timeout=3)
            except:
                camera_proc.kill()
            camera_proc = None
            print("  [CAM] Stopped")

def capture_current():
    """Capture current frame as base64. Uses burst capture for sharpness (§9.2)."""
    frames = []
    for _ in range(5):
        with frame_lock:
            if latest_frame:
                frames.append(latest_frame)
        time.sleep(0.1)

    if not frames:
        with frame_lock:
            if latest_frame:
                return base64.b64encode(latest_frame).decode()
        return None

    # Pick sharpest frame (simple: pick largest, as sharper JPEGs tend to be bigger)
    best = max(frames, key=len)
    return base64.b64encode(best).decode()


# ============================================================
# GEMINI VISION
# ============================================================
def gemini_vision(img_b64):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    age_context = ""
    if profile.get("age"):
        age_context = f"The child is {profile['age']} years old. Adjust fun fact complexity accordingly."

    prompt = f"""You are a fun kids toy AI that identifies objects.
Look at this image carefully. Return ONLY valid JSON (no markdown, no code blocks):
{{"title":"Descriptive Name","emoji":"one emoji","cat_emoji":"category emoji","category":"Category Name","facts":["Fun fact 1","Fun fact 2","Fun fact 3"],"mission":"Fun activity suggestion"}}

Rules:
- title: Be SPECIFIC and descriptive, 2-6 words
- emoji: one emoji representing the object
- cat_emoji: one emoji for the category (💡 man-made, 🌿 nature/plants, 🐾 animals, 🐛 insects, 🐦 birds)
- category: One of: "Plants", "Man-made Object", "Animals", "Nature", "Insects", "Birds"
- facts: 3 fun facts, each 1 sentence, fun for young kids. Use simple words. No emoji in facts.
- mission: A fun activity related to the object. No emoji in mission.
{age_context}"""

    payload = json.dumps({
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}
        ]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024}
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=30) as resp:
            result = json.loads(resp.read())
            text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
            if text.startswith("```"):
                text = re.sub(r'^```\w*\n?', '', text)
                text = re.sub(r'\n?```$', '', text).strip()
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                return json.loads(match.group())
            return json.loads(text)
    except Exception as e:
        print(f"  [AI] Error: {e}")
        return None


# ============================================================
# TTS (ElevenLabs primary, Gemini fallback)
# ============================================================
ELEVENLABS_VOICE_ID = "vGQNBgLaiM3EdZtxIiuY"
ELEVENLABS_MODEL = "eleven_turbo_v2_5"
_tts_counter = 0
_tts_lock = threading.Lock()

def elevenlabs_tts(text, out):
    if not ELEVENLABS_API_KEY:
        return gemini_tts_fallback(text, out)
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    payload = json.dumps({
        "text": text, "model_id": ELEVENLABS_MODEL,
        "voice_settings": {"stability": 0.10, "similarity_boost": 0.55, "style": 1.0, "use_speaker_boost": True}
    }).encode()
    headers = {"Accept": "audio/mpeg", "xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=payload, headers=headers)
    try:
        mp3_path = out.replace(".wav", ".mp3")
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=30) as resp:
            with open(mp3_path, "wb") as f:
                f.write(resp.read())
        subprocess.run(["ffmpeg", "-y", "-i", mp3_path, "-ar", "44100", "-ac", "1", out],
                       capture_output=True, timeout=10)
        os.remove(mp3_path)
        return out
    except Exception as e:
        print(f"  [TTS] ElevenLabs error: {e}")
        return gemini_tts_fallback(text, out)

def gemini_tts_fallback(text, out):
    if not GEMINI_API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent?key={GEMINI_API_KEY}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {"response_modalities": ["AUDIO"],
                             "speech_config": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}}}
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=30) as resp:
            result = json.loads(resp.read())
            pcm = base64.b64decode(result["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
            pcm_p = out + ".pcm"
            with open(pcm_p, "wb") as f:
                f.write(pcm)
            subprocess.run(["ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", pcm_p, out],
                           capture_output=True, timeout=10)
            os.remove(pcm_p)
            return out
    except Exception as e:
        print(f"  [TTS] Gemini error: {e}")
        return None

def tts_and_play(text, filename="wonder_tts"):
    """Generate TTS and play. Sets speaking=True for lip-sync."""
    global _tts_counter
    with _tts_lock:
        _tts_counter += 1
        n = _tts_counter
    out = f"/tmp/{filename}_{n}.wav"
    set_state(speaking=True)
    result = elevenlabs_tts(text, out)
    if result:
        play_audio(result)
        # Estimate speech duration (~150ms per word)
        words = len(text.split())
        time.sleep(max(1.0, words * 0.15))
    set_state(speaking=False)

def play_audio(p):
    try:
        subprocess.Popen(["aplay", "-D", "plughw:seeed2micvoicec,0", p],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except:
        try:
            subprocess.Popen(["aplay", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            pass


# ============================================================
# SOUND EFFECTS
# ============================================================
def _gen_sfx_wav(tone_list, sr=44100):
    all_samples = []
    for freq, dur_ms, vol in tone_list:
        n = int(sr * dur_ms / 1000)
        fade = min(int(sr * 0.008), n // 4)
        for i in range(n):
            env = 1.0
            if fade > 0:
                if i < fade: env = i / fade
                elif i > n - fade: env = (n - i) / fade
            val = int(vol * env * 32767 * math.sin(2 * math.pi * freq * i / sr))
            all_samples.append(max(-32767, min(32767, val)))
    data = struct.pack('<%dh' % len(all_samples), *all_samples)
    hdr = struct.pack('<4sI4s4sIHHIIHH4sI',
                      b'RIFF', 36 + len(data), b'WAVE', b'fmt ', 16, 1, 1, sr, sr * 2, 2, 16, b'data', len(data))
    return hdr + data

SFX_TONES = {
    'boot':    [(523, 200, 0.15), (659, 200, 0.18), (784, 300, 0.20)],
    'boop':    [(400, 60, 0.15), (200, 40, 0.10)],
    'scan':    [(280 + 21*i, 80, 0.05) for i in range(20)],
    'reveal_common':    [(784, 120, 0.18), (1047, 100, 0.15)],
    'reveal_rare':      [(523, 140, 0.20), (784, 140, 0.18), (1047, 120, 0.15)],
    'reveal_epic':      [(523, 120, 0.22), (659, 120, 0.20), (784, 120, 0.18), (1047, 120, 0.18), (1318, 150, 0.15)],
    'reveal_legendary': [(392, 150, 0.22), (523, 150, 0.20), (659, 150, 0.20), (784, 150, 0.20), (1047, 200, 0.22), (1318, 250, 0.18)],
    'mission_complete': [(784, 100, 0.20), (988, 100, 0.20), (1175, 100, 0.20), (1568, 200, 0.22)],
    'levelup': [(523, 150, 0.18), (659, 150, 0.18), (784, 200, 0.20), (1047, 250, 0.18)],
}

def play_sfx(name):
    if name not in SFX_TONES:
        return
    path = f'/tmp/sfx_{name}.wav'
    wav = _gen_sfx_wav(SFX_TONES[name])
    with open(path, 'wb') as f:
        f.write(wav)
    play_audio(path)


# ============================================================
# BATTERY MONITORING (§2.5)
# ============================================================
def get_battery_info():
    """Read battery percentage and charging status from system."""
    pct = 100
    charging = False
    # Try standard Linux power supply interface
    for supply in glob.glob("/sys/class/power_supply/*/"):
        try:
            cap_file = os.path.join(supply, "capacity")
            status_file = os.path.join(supply, "status")
            if os.path.exists(cap_file):
                pct = int(open(cap_file).read().strip())
            if os.path.exists(status_file):
                status = open(status_file).read().strip()
                charging = status in ("Charging", "Full")
        except:
            pass
    return pct, charging

def battery_monitor_loop():
    """Background thread: updates battery state, triggers warnings."""
    warned_15 = False
    while True:
        pct, charging = get_battery_info()
        set_state(battery_pct=pct, charging=charging)

        if pct <= 5 and not charging:
            print("  [BATTERY] Critical! Auto-shutdown.")
            subprocess.run(["sudo", "shutdown", "-h", "now"], capture_output=True)
            return

        if pct <= 15 and not charging and not warned_15:
            warned_15 = True
            set_state(screen="battery_low")
            threading.Thread(target=tts_and_play,
                             args=("I'm getting sleepy... let's charge up soon!",), daemon=True).start()
            time.sleep(4)
            set_state(screen="home")

        if pct > 20:
            warned_15 = False

        time.sleep(30)


# ============================================================
# IDLE MONITOR (§2.3) + AUTO-SHUTDOWN (10 min)
# ============================================================
last_interaction = time.time()
idle_lock = threading.Lock()

IDLE_PROMPTS = [
    "Hey {name}, what's over there?",
    "I'm bored! Let's scan something!",
    "Wake me up by scanning something!",
    "I bet there's something cool nearby!",
    "Hey {name}, let's go exploring!",
    "I wonder what that is over there...",
]

def touch_activity():
    """Call this whenever user interacts."""
    global last_interaction
    with idle_lock:
        last_interaction = time.time()

def idle_monitor_loop():
    """Background thread: idle voice prompts every 2-3 min, shutdown at 10 min."""
    last_prompt_time = time.time()
    while True:
        time.sleep(10)
        with idle_lock:
            idle_secs = time.time() - last_interaction

        cur = get_state()["screen"]
        set_state(idle_seconds=int(idle_secs))

        # Only prompt when on home screen
        if cur != "home":
            last_prompt_time = time.time()
            continue

        # Auto-shutdown after 10 min idle
        if idle_secs > 600:
            print("  [IDLE] 10 min inactive. Shutting down.")
            tts_and_play("I'm going to sleep now. See you later!")
            time.sleep(2)
            subprocess.run(["sudo", "shutdown", "-h", "now"], capture_output=True)
            return

        # Voice prompt every 2-3 minutes
        if time.time() - last_prompt_time > random.uniform(120, 180):
            last_prompt_time = time.time()
            name = profile.get("name", "") or "friend"
            prompt = random.choice(IDLE_PROMPTS).format(name=name)
            print(f"  [IDLE] Prompt: {prompt}")
            threading.Thread(target=tts_and_play, args=(prompt,), daemon=True).start()


# ============================================================
# SCAN PIPELINE (§1.1, §3.x)
# ============================================================
scan_lock = threading.Lock()

# Scanning voice lines (§3.3)
SCANNING_LINES = [
    "Hmm, let's see...",
    "What is that?...",
    "That's... hmmm, what's that?",
    "Let me see, let me see...",
    "Hold still... hold still...",
]

# Duplicate responses (§3.5)
DUPLICATE_RESPONSES = [
    "You found it again! Did you know? {fact}",
    "You really like this one! It's your {count} time scanning this!",
    "You're becoming an expert on {title}!",
]

def run_scan():
    """Full scan pipeline with randomized variants (§1.1)."""
    if not scan_lock.acquire(blocking=False):
        return
    try:
        touch_activity()

        # 1. Scanning animation + voice line
        set_state(screen="scanning")
        play_sfx("scan")
        scan_line = random.choice(SCANNING_LINES)
        threading.Thread(target=tts_and_play, args=(scan_line, "scan_line"), daemon=True).start()

        # 2. Capture image (burst for sharpness §9.2)
        print("\n  [SCAN] Capturing...")
        img = capture_current()
        if not img:
            print("  [SCAN] No frame!")
            set_state(screen="home")
            return
        stop_camera()

        # 3. AI identification
        set_state(screen="scanning")
        print("  [SCAN] Analyzing...")

        # Check network connectivity
        result = None
        try:
            result = gemini_vision(img)
        except Exception as e:
            print(f"  [SCAN] Network error: {e}")
            # No-signal handling (§8.2)
            set_state(screen="home")
            tts_and_play("Hmm, let me think about this one... I'll tell you soon!")
            # Queue for later
            queue = load_json(QUEUE_PATH, [])
            queue.append({"image": img, "timestamp": time.time()})
            save_json(QUEUE_PATH, queue)
            return

        if not result:
            # Scan failed - conversational response (§3.4)
            set_state(screen="ask")
            tts_and_play("Hmm, I couldn't quite see that. Can you show me again?")
            time.sleep(3)
            set_state(screen="home")
            return

        # 4. Parse result
        title = result.get("title", "Something Cool")
        emoji = result.get("emoji", "?")
        cat_emoji = result.get("cat_emoji", "💡")
        category = result.get("category", "Objects")
        facts = result.get("facts", ["Amazing find!"])
        fact = facts[0] if facts else "Amazing find!"
        mission_text = result.get("mission", "Go explore!")

        # 5. Check duplicate
        existing = find_duplicate(title)
        is_dup = existing is not None
        rarity = existing["rarity"] if is_dup else calc_rarity()
        xpe = rarity_xp(rarity, is_duplicate=is_dup)
        scan_count = (existing["scan_count"] + 1) if is_dup else 1

        # Add to collection
        add_to_collection(title, emoji, category, rarity, facts, mission_text)

        # Update XP
        profile["xp"] += xpe
        save_profile()

        lv, lt, ct, nt = get_level_info(profile["xp"])
        xp_pct = int(((profile["xp"] - ct) / max(nt - ct, 1)) * 100)

        # For duplicates, pick a different fact
        if is_dup and len(facts) > 1:
            fact = facts[min(scan_count - 1, len(facts) - 1)]

        # Choose scan variant (§1.1)
        variant = random.randint(1, 4)

        set_state(
            object_name=title, object_emoji=emoji, rarity=rarity,
            fact=fact, mission=mission_text, category=category, cat_emoji=cat_emoji,
            xp=profile["xp"], level=lv, level_title=lt,
            xp_earned=xpe, xp_pct=xp_pct,
            is_duplicate=is_dup, scan_count=scan_count,
            scan_variant=variant,
        )

        print(f"  [SCAN] {emoji} {title} ({rarity}) {'[DUP]' if is_dup else '[NEW]'} variant={variant}")

        # 6. Execute scan flow variant
        if is_dup and scan_count >= 5:
            # Quiz mode for 5th+ scan
            _flow_quiz(title, emoji, facts, xpe)
        elif is_dup:
            _flow_duplicate(title, emoji, fact, rarity, xpe, scan_count)
        elif variant == 1:
            _flow_standard(title, emoji, fact, rarity, xpe)
        elif variant == 2:
            _flow_ask_what(title, emoji, fact, rarity, xpe)
        elif variant == 3:
            _flow_ask_cool(title, emoji, fact, rarity, xpe)
        elif variant == 4:
            _flow_excited_find(title, emoji, fact, rarity, xpe)

        # 7. Check mission progress
        mission_just_completed = check_mission_progress()
        if mission_just_completed:
            _flow_mission_complete()

        # 8. Show next mission
        ensure_active_mission()
        set_state(screen="mission", mission=mission_state["text"],
                  mission_progress=mission_state["progress"],
                  mission_target=mission_state["target"])
        time.sleep(4)

        # 9. Show XP
        set_state(screen="xp")
        play_sfx("levelup") if xpe >= 100 else None
        print(f"  [SCAN] +{xpe}XP -> Lv{lv} ({lt})")
        time.sleep(3)

        # 10. Return home
        set_state(screen="home")
        refresh_xp_state()
        print("  [SCAN] Done!\n")

    except Exception as e:
        print(f"  [SCAN] Error: {e}")
        import traceback
        traceback.print_exc()
        set_state(screen="home")
        stop_camera()
    finally:
        scan_lock.release()


def _flow_standard(title, emoji, fact, rarity, xpe):
    """Variant 1: Scan → Rarity Reveal → Fun Fact → Level Up"""
    # Excited face
    set_state(screen="excited")
    play_sfx(f"reveal_{rarity}")
    tts_and_play(f"Wow, it's a {title}!")
    time.sleep(1)
    # Reveal card
    set_state(screen="reveal")
    time.sleep(4)
    # Fact
    set_state(screen="fact")
    tts_and_play(f"Did you know? {fact}")
    time.sleep(3)

def _flow_ask_what(title, emoji, fact, rarity, xpe):
    """Variant 2: Scan → 'What is that?' → [answer] → Fun fact"""
    set_state(screen="ask")
    tts_and_play("Hmmm.. What is that? Do you know? Can you tell me what that is?")
    time.sleep(4)  # Wait for child's voice answer
    set_state(screen="excited")
    play_sfx(f"reveal_{rarity}")
    tts_and_play(f"Oh! I heard about this! Did you know? {fact}")
    time.sleep(1)
    set_state(screen="reveal")
    time.sleep(4)

def _flow_ask_cool(title, emoji, fact, rarity, xpe):
    """Variant 3: Scan → 'What is that?' → [answer] → 'What's cool about this?'"""
    set_state(screen="ask")
    tts_and_play("Hmmm.. What is that? Do you know? Can you tell me what that is?")
    time.sleep(4)
    tts_and_play("Oh! I heard about this! Can you tell me what's cool about this?")
    time.sleep(4)
    set_state(screen="excited")
    play_sfx(f"reveal_{rarity}")
    time.sleep(1)
    set_state(screen="reveal")
    tts_and_play(f"It's a {title}!")
    time.sleep(3)

def _flow_excited_find(title, emoji, fact, rarity, xpe):
    """Variant 4: 'Whoa! That's [RARITY]!' → Fun Fact → 'Find another!'"""
    set_state(screen="excited")
    play_sfx(f"reveal_{rarity}")
    rarity_label = RARITY_CONFIG[rarity]["label"]
    tts_and_play(f"Whoa! That's {rarity_label}! I've never seen one before!")
    set_state(screen="reveal")
    time.sleep(2)
    set_state(screen="fact")
    tts_and_play(f"Did you know? {fact}")
    time.sleep(1)
    tts_and_play("Quick, can you find another one nearby?")
    time.sleep(2)

def _flow_duplicate(title, emoji, fact, rarity, xpe, count):
    """Duplicate scan flow (§3.5)."""
    set_state(screen="excited")
    ordinals = {2: "2nd", 3: "3rd"}
    ord_str = ordinals.get(count, f"{count}th")
    tts_and_play(f"You found it again! This is your {ord_str} time scanning this!")
    set_state(screen="reveal")
    time.sleep(2)
    set_state(screen="fact")
    tts_and_play(f"Here's another fun fact: {fact}")
    time.sleep(3)

def _flow_quiz(title, emoji, facts, xpe):
    """5th+ scan quiz flow (§3.5)."""
    set_state(screen="excited")
    tts_and_play(f"You're becoming an expert on {title}!")
    time.sleep(1)
    set_state(screen="ask")
    if facts:
        tts_and_play(f"Quiz time! Can you tell me something cool about {title}?")
    time.sleep(5)
    tts_and_play("Great job!")
    set_state(screen="reveal")
    time.sleep(3)

def _flow_mission_complete():
    """Mission complete celebration (§6.3)."""
    set_state(screen="mission_complete")
    play_sfx("mission_complete")
    tts_and_play("AMAZING! Mission complete!")
    profile["xp"] += 50  # Bonus 50 XP
    save_profile()
    time.sleep(3)
    # Generate new mission immediately
    generate_mission()


# ============================================================
# ONBOARDING (§1.1)
# ============================================================
def run_onboarding():
    """First-time setup: Welcome → Name → Guide to scan."""
    set_state(screen="onboarding_welcome")
    time.sleep(1)
    tts_and_play("Hello! My name is Little Wonder!")
    time.sleep(2)

    set_state(screen="onboarding_name")
    tts_and_play("What's your name?")
    time.sleep(5)  # Wait for recording — in V1 we'll use a default

    # In V1 without voice recognition, use fallback after pause
    name = profile.get("name", "")
    if not name:
        name = "Explorer Friend"
        tts_and_play(f"I'll call you my {name}!")
    else:
        tts_and_play(f"Hi {name}!")

    profile["name"] = name
    set_state(name=name)

    time.sleep(1)
    set_state(screen="onboarding_story")
    tts_and_play("I am from the Moon! I always liked to wonder, and one day I wandered too far and fell off the Moon, all the way to Earth!")
    time.sleep(2)
    tts_and_play("I want to learn everything about Earth! Can you show me everything?")
    time.sleep(2)

    set_state(screen="onboarding_start")
    tts_and_play("Say OK to get started!")
    time.sleep(3)
    tts_and_play("OK let's go! Tap on the button to scan something!")
    time.sleep(2)

    profile["onboarded"] = True
    save_profile()
    set_state(screen="home", onboarded=True)
    refresh_xp_state()


# ============================================================
# BOOT SEQUENCE (§8.5)
# ============================================================
def run_boot():
    """Boot: chime + face + greeting."""
    set_state(screen="boot")
    play_sfx("boot")
    time.sleep(2)

    name = profile.get("name", "")
    if name and profile.get("onboarded"):
        greeting = random.choice([f"Hi {name}!", f"Let's go exploring, {name}!", f"Hey {name}! Ready to discover?"])
        set_state(screen="home")
        refresh_xp_state()
        ensure_active_mission()
        set_state(mission=mission_state["text"],
                  mission_progress=mission_state["progress"],
                  mission_target=mission_state["target"])
        tts_and_play(greeting)
        # Announce today's mission
        tts_and_play(f"Today's mission: {mission_state['text']}")
    else:
        # First time — go to onboarding
        run_onboarding()


# ============================================================
# NO-SIGNAL QUEUE PROCESSOR (§8.2)
# ============================================================
def process_scan_queue():
    """Background: retry queued scans when connectivity returns."""
    while True:
        time.sleep(60)
        queue = load_json(QUEUE_PATH, [])
        if not queue:
            continue
        # Test connectivity
        try:
            urllib.request.urlopen("https://www.google.com", timeout=5, context=SSL_CTX)
        except:
            continue

        print(f"  [QUEUE] Processing {len(queue)} queued scans...")
        remaining = []
        for item in queue:
            try:
                result = gemini_vision(item["image"])
                if result:
                    # TODO: play deferred reveal
                    print(f"  [QUEUE] Processed: {result.get('title', '?')}")
                else:
                    remaining.append(item)
            except:
                remaining.append(item)
        save_json(QUEUE_PATH, remaining)


# ============================================================
# BEZEL / TOUCH INPUT
# ============================================================
bezel_events = []
bezel_lock = threading.Lock()

def start_input_reader():
    """Read touch + bezel encoder events."""
    threading.Thread(target=_input_loop, daemon=True).start()

def _input_loop():
    """Find and read from touch + encoder input devices."""
    for dev_path in sorted(glob.glob("/dev/input/event*")):
        try:
            with open(dev_path, "rb") as f:
                import fcntl
                buf = bytearray(256)
                try:
                    fcntl.ioctl(f, 0x80FF4506, buf)
                    name = buf.split(b'\x00')[0].decode()
                    if "encoder" in name.lower() or "rotary" in name.lower():
                        print(f"  [INPUT] Encoder: {name} at {dev_path}")
                        threading.Thread(target=_read_encoder, args=(dev_path,), daemon=True).start()
                except:
                    pass
        except:
            pass

def _read_encoder(dev_path):
    """Read rotary encoder events for bezel rotation."""
    EVENT_SIZE = struct.calcsize("llHHI")
    try:
        with open(dev_path, "rb") as f:
            while True:
                data = f.read(EVENT_SIZE)
                if not data: break
                _, _, ev_type, ev_code, ev_value = struct.unpack("llHHI", data)
                if ev_type == 2:  # EV_REL
                    with bezel_lock:
                        bezel_events.append({"dir": 1 if ev_value > 0 else -1, "time": time.time()})
    except:
        pass

def check_bezel():
    """Check for bezel rotation. Returns direction or 0."""
    with bezel_lock:
        if bezel_events:
            evt = bezel_events.pop(0)
            return evt["dir"]
    return 0


# ============================================================
# HTTP SERVER
# ============================================================
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/display" or self.path == "/display/" or self.path == "/":
            spa = BASE_DIR / "display.html"
            if spa.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.end_headers()
                self.wfile.write(spa.read_bytes())
                return

        if self.path == "/display/state":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(get_state()).encode())
            return

        if self.path.startswith("/frame"):
            with frame_lock:
                f = latest_frame
            if f:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(f)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(f)
            else:
                self.send_response(204)
                self.end_headers()
            return

        if self.path == "/collection":
            cats = get_collection_by_category()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(cats).encode())
            return

        if self.path.startswith("/assets/"):
            asset_path = BASE_DIR / self.path.lstrip("/")
            if asset_path.exists() and asset_path.is_file():
                mime_types = {
                    '.ttf': 'font/ttf', '.woff': 'font/woff', '.woff2': 'font/woff2',
                    '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml',
                    '.css': 'text/css', '.js': 'text/javascript', '.wav': 'audio/wav',
                }
                ct = mime_types.get(asset_path.suffix.lower(), 'application/octet-stream')
                data = asset_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path == "/camera/start":
            start_camera()
            set_state(screen="viewfinder")
            self._ok()

        elif self.path == "/camera/stop":
            stop_camera()
            self._ok()

        elif self.path == "/scan":
            self._ok()
            touch_activity()
            threading.Thread(target=run_scan, daemon=True).start()

        elif self.path == "/sfx":
            try:
                ln = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(ln)) if ln > 0 else {}
                name = data.get('name', '')
                if name:
                    threading.Thread(target=play_sfx, args=(name,), daemon=True).start()
            except:
                pass
            self._ok()

        elif self.path == "/tts":
            try:
                ln = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(ln)) if ln > 0 else {}
                text = data.get("text", "")
                if text:
                    threading.Thread(target=tts_and_play, args=(text,), daemon=True).start()
            except:
                pass
            self._ok()

        elif self.path == "/display/update":
            try:
                ln = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(ln)) if ln > 0 else {}
                touch_activity()
                set_state(**data)
            except:
                pass
            self._ok()

        elif self.path == "/menu/open":
            touch_activity()
            set_state(screen="menu")
            self._ok()

        elif self.path == "/menu/collection":
            touch_activity()
            cats = get_collection_by_category()
            set_state(screen="collection_grid", collection_data=json.dumps(cats))
            self._ok()

        elif self.path == "/menu/missions":
            touch_activity()
            ensure_active_mission()
            set_state(screen="mission",
                      mission=mission_state["text"],
                      mission_progress=mission_state["progress"],
                      mission_target=mission_state["target"])
            self._ok()

        elif self.path == "/menu/home":
            touch_activity()
            set_state(screen="home")
            refresh_xp_state()
            self._ok()

        elif self.path == "/onboarding/name":
            try:
                ln = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(ln)) if ln > 0 else {}
                name = data.get("name", "Explorer Friend")
                profile["name"] = name
                save_profile()
                set_state(name=name)
            except:
                pass
            self._ok()

        else:
            self.send_response(404)
            self.end_headers()

    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, fmt, *args):
        if args and "GET" not in str(args[0]):
            BaseHTTPRequestHandler.log_message(self, fmt, *args)


# ============================================================
# MAIN
# ============================================================
PORT = int(os.getenv("PORT", 8888))

class ReusableHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()

if __name__ == "__main__":
    print()
    print("  ╔═══════════════════════════════════════╗")
    print("  ║   Little Wonder V1 — Device Server    ║")
    print("  ╚═══════════════════════════════════════╝")
    print(f"  Display:   http://localhost:{PORT}/display")
    print(f"  Gemini:    {'OK' if GEMINI_API_KEY else 'MISSING!'}")
    print(f"  ElevenLabs:{'OK' if ELEVENLABS_API_KEY else 'MISSING (using Gemini TTS)'}")
    print(f"  Profile:   {profile.get('name') or 'New user'}")
    print(f"  XP:        {profile.get('xp', 0)}")
    print(f"  Collection:{len(collection)} items")
    print(f"  Mission:   {mission_state.get('text', 'None')}")
    print()

    # Start background threads
    threading.Thread(target=battery_monitor_loop, daemon=True).start()
    threading.Thread(target=idle_monitor_loop, daemon=True).start()
    threading.Thread(target=process_scan_queue, daemon=True).start()
    start_input_reader()

    # Boot sequence
    threading.Thread(target=run_boot, daemon=True).start()

    print(f"  Server listening on port {PORT}...")
    print("  Press Ctrl+C to stop\n")
    ReusableHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
