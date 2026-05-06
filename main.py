import os
import uuid
import sqlite3
import time
import glob
from typing import Optional
from fastapi import FastAPI, HTTPException, Form, Depends, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from dotenv import load_dotenv
from openai import OpenAI
from authlib.integrations.starlette_client import OAuth
from starlette.config import Config
import requests
import re
import PIL.Image
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

from moviepy.editor import AudioFileClip, ImageClip, TextClip, CompositeVideoClip, ColorClip
from moviepy.config import change_settings
import platform
import tempfile

if platform.system() == "Darwin":
    # M1/M2/M3 Mac (Apple Silicon) の Homebrew 標準パスを指定
    change_settings({"IMAGEMAGICK_BINARY": "/opt/homebrew/bin/magick"})
    # ImageMagick の一時ディレクトリを明示的に指定して安定させる
    os.environ["MAGICK_TEMPORARY_PATH"] = tempfile.gettempdir()

import stripe


# .envファイルから環境変数を読み込む
load_dotenv()

# ==== App Setup ====
app = FastAPI(
    title="Faceless Video Generator SaaS API",
    description="TikTok/Shorts向けの顔出しなし動画を全自動生成するSaaS向けAPI",
    version="2.0.0"
)

# セッションミドルウェア (OAuthに必須)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("STRIPE_WEBHOOK_SECRET") or "super_secret_key_for_oauth", max_age=86400*7) # 1 week session

# CORS対応 (フロントエンドからのアクセスを許可)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# レートリミット (Slowapi) の設定
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.get("/")
@app.head("/")
async def serve_index():
    # ヘルスチェックやブラウザからのアクセス時にindex.htmlを返す
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"status": "ok", "message": "SnappVid API is running"}

# セキュリティ設定
DISPOSABLE_DOMAINS = {"mailinator.com", "tempmail.com", "10minutemail.com", "yopmail.com", "guerrillamail.com", "throwawaymail.com", "temp-mail.org", "trashmail.com", "getnada.com"}
active_generation_users = set()

# ジョブ状態のトラッキング用
job_status = {}

def sanitize_theme(theme: str) -> str:
    sanitized = theme.replace('\n', ' ').replace('\r', '')
    # OSコマンドインジェクションやディレクトリトラバーサルを防ぐため特殊文字を削除
    sanitized = re.sub(r'[<>{}\[\]|;&]', '', sanitized)
    sanitized = sanitized.replace('../', '').replace('..\\', '')
    import html
    sanitized = html.escape(sanitized)
    return sanitized.strip()[:100]

def is_valid_email(email: str) -> bool:
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return re.match(pattern, email) is not None

# 生成された動画を返すための静的ファイルマウント
os.makedirs("output", exist_ok=True)
app.mount("/files", StaticFiles(directory="output"), name="files")

# ==== Database Setup (sqlite3) ====
DB_FILE = "database.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan_status TEXT DEFAULT 'Free',
            credits INTEGER DEFAULT 1,
            is_verified INTEGER DEFAULT 0,
            otp_code TEXT
        )
    ''')
    
    # ユーザーテーブルの拡張
    cursor.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'last_video_url' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN last_video_url TEXT")
    
    # ジョブ追跡テーブル
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS video_jobs (
            job_id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER DEFAULT 0,
            message TEXT,
            video_url TEXT,
            error TEXT,
            created_at INTEGER NOT NULL
        )
    ''')
        
    conn.commit()
    conn.close()

init_db()

def cleanup_old_files():
    try:
        now = time.time()
        cutoff = now - 86400  # 24 hours ago
        for file_path in glob.glob("output/*"):
            if os.path.isfile(file_path):
                if os.path.getmtime(file_path) < cutoff:
                    os.remove(file_path)
                    print(f"Cleaned up old file: {file_path}")
            elif os.path.isdir(file_path):
                if os.path.getmtime(file_path) < cutoff:
                    import shutil
                    shutil.rmtree(file_path)
                    print(f"Cleaned up old directory: {file_path}")
        
        # ImageMagick が残した古い一時ファイル (/var/folders/... などの magick-*) を削除
        temp_dir = tempfile.gettempdir()
        for file_path in glob.glob(os.path.join(temp_dir, "magick-*")):
            if os.path.isfile(file_path):
                # 処理の競合を防ぐため、1時間以上前のものを削除対象とする
                if os.path.getmtime(file_path) < (now - 3600):
                    os.remove(file_path)
                    print(f"Cleaned up ImageMagick temp file: {file_path}")
                    
    except Exception as e:
        print(f"Cleanup error: {e}")

# ==== Config & API Keys ====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID_STARTER = os.getenv("STRIPE_PRICE_ID_STARTER")
STRIPE_PRICE_ID_PRO_ANNUAL = os.getenv("STRIPE_PRICE_ID_PRO_ANNUAL")
STRIPE_PRICE_ID_TOPUP = os.getenv("STRIPE_PRICE_ID_TOPUP")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# Clients Initialize
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
if STRIPE_API_KEY:
    stripe.api_key = STRIPE_API_KEY

# OAuth Configuration
config = Config(environ=os.environ)
oauth = OAuth(config)

if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name='google',
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={
            'scope': 'openid email profile'
        }
    )



# ==== Models ====
class VideoRequest(BaseModel):
    theme: str = Field(..., max_length=100, description="The topic for the video (max 100 chars)")
    email: str

class VideoResponse(BaseModel):
    status: str
    message: str
    job_id: str
    theme: str
    
class VideoStatusResponse(BaseModel):
    status: str
    progress: int
    message: str
    video_url: Optional[str] = None
    script: Optional[str] = None
    local_path: Optional[str] = None

class AuthResponse(BaseModel):
    status: str
    message: str
    user_id: Optional[str] = None
    email: Optional[str] = None
    plan_status: Optional[str] = None
    credits: Optional[int] = None



@app.get("/login")
async def login_shortcut():
    return RedirectResponse(url="/auth/google/login")

@app.get('/logout')
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")

@app.get('/auth/google/login')
async def google_login(request: Request):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Google OAuth is not configured on the server.")
    
    # Redirect URI is the callback endpoint
    redirect_uri = request.url_for('google_callback')
    # Fix for environments behind proxy (like Render)
    if "localhost" not in str(redirect_uri):
        redirect_uri = str(redirect_uri).replace("http://", "https://")
    
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get('/auth/google/callback')
async def google_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get('userinfo')
        if not user_info:
            raise HTTPException(status_code=400, detail="Failed to fetch user info from Google.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth verification failed: {str(e)}")

    email = user_info.get('email')
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, plan_status, credits FROM users WHERE email = ?", (email,))
    user = cursor.fetchone()
    
    if not user:
        # Create new user
        user_id = str(uuid.uuid4())
        # Provide an empty string since they use OAuth
        cursor.execute(
            "INSERT INTO users (id, email, password_hash, plan_status, credits, is_verified) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, email, "", "Free", 10, 1) # Auto-verified since it's Google
        )
        conn.commit()
        credits = 10
    else:
        user_id, plan_status, credits = user[0], user[1], user[2]
        # Ensure is_verified is 1 for existing users who login via Google
        cursor.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (user_id,))
        conn.commit()
        
    conn.close()
    
    # セッションにユーザー情報を保存
    request.session["user"] = {"email": email, "id": user_id}
    
    # Redirect back to index.html
    return RedirectResponse(url=f"/?oauth=success&email={email}&credits={credits}")

# ==== Stripe Endpoints ====
@app.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        else:
            # シークレットがない場合はモック検証
            import json
            event = json.loads(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 決済成功時の処理
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        email = session.get('client_reference_id')
        amount_total = session.get('amount_total', 0)
        
        plan_type = None
        if amount_total == 1999:
            plan_type = "Starter"
        elif amount_total == 19999:
            plan_type = "Pro"
        elif amount_total == 999:
            plan_type = "Top-up"
        else:
            # Fallback based on metadata if any
            metadata = session.get('metadata', {})
            plan_type = metadata.get('type')
            if not email:
                email = metadata.get('user_id')
        
        print(f"Webhook received: email={email}, amount_total={amount_total}, deduced plan={plan_type}")
        
        if email and plan_type:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            if plan_type == "Starter":
                cursor.execute("UPDATE users SET plan_status = 'Starter', credits = 300 WHERE email = ?", (email,))
            elif plan_type == "Pro":
                cursor.execute("UPDATE users SET plan_status = 'Pro', credits = 500 WHERE email = ?", (email,))
            elif plan_type == "Top-up":
                cursor.execute("UPDATE users SET credits = credits + 100 WHERE email = ?", (email,))
                
            conn.commit()
            conn.close()
            print(f"User {email} updated for plan: {plan_type}.")
        else:
            print(f"Failed to update user. email or plan_type is missing.")

    return {"status": "success"}

# ==== Video Generation Endpoint ====
@app.post("/generate-video", response_model=VideoResponse)
@limiter.limit("3/minute")
async def generate_video(request: Request, body: VideoRequest, background_tasks: BackgroundTasks):
    if not openai_client:
        raise HTTPException(status_code=500, detail="OpenAI API Key is not configured properly.")

    theme = sanitize_theme(body.theme)
    email = body.email
    
    if email in active_generation_users:
        raise HTTPException(status_code=429, detail="A video generation is already in progress for this account.")

    cleanup_old_files()

    # ==== クレジット確認のみ（消費は成功時） ====
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, plan_status, credits FROM users WHERE email = ?", (email,))
    user = cursor.fetchone()
    
    if not user:
        conn.close()
        raise HTTPException(status_code=401, detail="User not found. Please login.")
        
    user_id, plan_status, credits = user[0], user[1], user[2]
    is_special_trial = (email == "moriretsu06@gmail.com")

    if credits < 10 and not is_special_trial:
        conn.close()
        raise HTTPException(status_code=403, detail="Credit limit reached. Please recharge.")
    
    conn.close()
    
    is_pro = (plan_status == "Pro")
    active_generation_users.add(email)
    
    job_id = str(uuid.uuid4())
    job_status[job_id] = {
        "status": "processing",
        "progress": 0,
        "message": "Initializing...",
        "video_url": None,
        "script": None,
        "local_path": None,
        "error": None,
        "created_at": time.time()
    }
    
    # データベースにジョブを登録 (永続化)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO video_jobs (job_id, email, status, progress, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, email, "processing", 0, "Initializing...", time.time())
    )
    conn.commit()
    conn.close()
    
    # ==== バックグラウンドタスクの登録 ====
    background_tasks.add_task(process_video_background, job_id, theme, email, user_id, is_pro)

    return VideoResponse(
        status="success",
        message="Video generation started in background.",
        job_id=job_id,
        theme=theme
    )

def update_job_db(job_id: str, status: str = None, progress: int = None, message: str = None, video_url: str = None, error: str = None):
    """データベース上のジョブステータスを更新する"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    updates = []
    params = []
    if status:
        updates.append("status = ?")
        params.append(status)
    if progress is not None:
        updates.append("progress = ?")
        params.append(progress)
    if message:
        updates.append("message = ?")
        params.append(message)
    if video_url:
        updates.append("video_url = ?")
        params.append(video_url)
    if error:
        updates.append("error = ?")
        params.append(error)
    
    if updates:
        sql = f"UPDATE video_jobs SET {', '.join(updates)} WHERE job_id = ?"
        params.append(job_id)
        cursor.execute(sql, params)
        conn.commit()
    conn.close()
    
    # メモリ上のキャッシュも同期（高速アクセスのため）
    if job_id in job_status:
        if status: job_status[job_id]["status"] = status
        if progress is not None: job_status[job_id]["progress"] = progress
        if message: job_status[job_id]["message"] = message
        if video_url: job_status[job_id]["video_url"] = video_url
        if error: job_status[job_id]["error"] = error

@app.get("/generation-status/{job_id}", response_model=VideoStatusResponse)
async def get_generation_status(job_id: str):
    # まずメモリをチェック
    if job_id in job_status:
        status_data = job_status[job_id]
    else:
        # メモリになければDBをチェック（サーバー再起動対策）
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT status, progress, message, video_url, error FROM video_jobs WHERE job_id = ?", (job_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Job ID not found.")
        status_data = {
            "status": row[0],
            "progress": row[1],
            "message": row[2],
            "video_url": row[3],
            "error": row[4],
            "script": None, # DBには保存していない
            "local_path": None
        }
        
    if status_data["status"] == "error":
        raise HTTPException(status_code=500, detail=status_data.get("error", "Unknown error occurred."))
        
    return VideoStatusResponse(
        status=status_data["status"],
        progress=status_data["progress"],
        message=status_data["message"],
        video_url=status_data["video_url"],
        script=status_data.get("script"),
        local_path=status_data.get("local_path")
    )

@app.get("/api/active-job")
async def get_active_job(request: Request):
    user_info = request.session.get("user")
    if not user_info:
        return {"job_id": None}
    
    email = user_info["email"]
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # 直近1時間以内の実行中ジョブを探す
    cursor.execute(
        "SELECT job_id FROM video_jobs WHERE email = ? AND status = 'processing' AND created_at > ? ORDER BY created_at DESC LIMIT 1", 
        (email, time.time() - 3600)
    )
    row = cursor.fetchone()
    conn.close()
    return {"job_id": row[0] if row else None}

@app.get("/api/user-profile")
async def get_user_profile(request: Request):
    user_info = request.session.get("user")
    if not user_info:
        raise HTTPException(status_code=401, detail="Not logged in")
    
    email = user_info["email"]
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT credits, plan_status, last_video_url FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
        
    return {
        "email": email,
        "credits": row[0],
        "plan": row[1],
        "last_video_url": row[2]
    }

import threading

def update_progress_gradually(job_id: str, current: int, target: int, duration_sec: int, stop_event: threading.Event):
    """
    徐々に進捗率を上げるためのヘルパー関数。外部APIなどの長い待機中にフリーズしているように見せないため。
    """
    step_time = duration_sec / (target - current) if target > current else 1.0
    progress = current
    while progress < target and not stop_event.is_set():
        time.sleep(step_time)
        if stop_event.is_set():
            break
        progress += 1
        update_job_db(job_id, progress=progress)

def process_video_background(job_id: str, theme: str, email: str, user_id: str, is_pro: bool):
    stop_event = threading.Event()
    progress_thread = None

    def start_pseudo_progress(current: int, target: int, expected_duration: int):
        nonlocal progress_thread, stop_event
        if progress_thread and progress_thread.is_alive():
            stop_event.set()
            progress_thread.join()
        stop_event.clear()
        progress_thread = threading.Thread(
            target=update_progress_gradually, 
            args=(job_id, current, target, expected_duration, stop_event)
        )
        progress_thread.start()

    def stop_pseudo_progress(final_val: int):
        nonlocal progress_thread, stop_event
        if progress_thread and progress_thread.is_alive():
            stop_event.set()
            progress_thread.join()
        update_job_db(job_id, progress=final_val)

    try:
        timestamp = int(time.time())
        # セキュリティ強化のため推測不可能なファイル名にする
        unique_id = str(uuid.uuid4())[:8]
        video_filename = f"video_{unique_id}_{timestamp}.mp4"
        
        audio_path = os.path.join("output", f"audio_{job_id}.mp3")
        bg_image_path = os.path.join("output", f"bg_{job_id}.png")
        output_mp4_path = os.path.join("output", video_filename)

        # 1. 台本生成: OpenAI API (GPT-4o)
        update_job_db(job_id, message="Generating script...")
        start_pseudo_progress(0, 15, 10)
        print(f"[{job_id}] STEP 1: Generating script...")
        
        system_prompt = "あなたはTikTok/Shorts向けの短尺動画の台本ライターです。"
        if is_pro:
            system_prompt += "指定されたテーマについて、30〜45秒程度で読める、詳細で魅力的なネイティブ英語の台本を作成してください。"
        else:
            system_prompt += "指定されたテーマについて、15〜20秒程度で読める、短く簡潔なネイティブ英語の台本を作成してください。"
            
        system_prompt += "余計な挨拶や説明は省き、ナレーションの英語テキストのみを出力してください。"
        
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"テーマ: {theme}"}
            ],
            timeout=30.0
        )
        script = response.choices[0].message.content
        if job_id in job_status: job_status[job_id]["script"] = script
        stop_pseudo_progress(15)
        print(f"[{job_id}] Script generated:\n{script}\n")

        # 2. 音声生成: OpenAI TTS API
        update_job_db(job_id, message="Generating voice (OpenAI)...")
        start_pseudo_progress(15, 45, 15)
        print(f"[{job_id}] STEP 2: Generating audio via OpenAI TTS...")
        
        try:
            with openai_client.with_streaming_response.audio.speech.create(
                model="tts-1",
                voice="onyx",
                input=script,
                timeout=60.0
            ) as response_audio:
                response_audio.stream_to_file(audio_path)
        except Exception as e:
            print(f"[{job_id}] TTS Error: {e}")
            raise Exception(f"OpenAI TTS API Error: {str(e)}")
        
        stop_pseudo_progress(45)
        print(f"[{job_id}] Audio saved. Length: {duration if 'duration' in locals() else 'unknown'}")

        # 3. 背景画像の生成とダウンロード: DALL-E 3
        update_job_db(job_id, message="Generating background image (DALL-E 3)...")
        start_pseudo_progress(45, 65, 15)
        print(f"[{job_id}] STEP 3: Generating background image...")
        
        image_response = openai_client.images.generate(
            model="dall-e-3",
            prompt=theme,
            size="1024x1792",
            quality="standard",
            n=1,
            timeout=60.0
        )
        image_url = image_response.data[0].url
        img_data = requests.get(image_url, timeout=30.0).content
        with open(bg_image_path, "wb") as f:
            f.write(img_data)
        
        stop_pseudo_progress(65)
        job_status[job_id]["message"] = "Synthesizing video (This may take a while)..."
        start_pseudo_progress(65, 95, 40) # 65% から 95% まで約40秒かけて進む
        
        # 4. 字幕(テロップ)の分割と合成
        raw_phrases = [p.strip() for p in re.split(r'(?<=[.!?]) +', script) if p.strip()]
        if not raw_phrases:
            raw_phrases = [script]
            
        phrases = []
        for rp in raw_phrases:
            words = rp.split()
            for i in range(0, len(words), 7):
                phrases.append(" ".join(words[i:i+7]))
            
        audio_clip = AudioFileClip(audio_path)
        duration = audio_clip.duration
        total_chars = sum(len(p.replace(" ", "")) for p in phrases)
        text_clips = []
        current_time = 0
        
        # フォントの選択 (Mac/Linux両対応)
        selected_font = "Arial"
        if platform.system() == "Darwin":
            selected_font = "/System/Library/Fonts/Supplemental/Arial.ttf"
        else:
            # RenderなどのLinux環境での一般的なフォントパス
            possible_fonts = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
                "Arial-Bold",
                "Helvetica-Bold"
            ]
            for fpath in possible_fonts:
                if os.path.exists(fpath):
                    selected_font = fpath
                    break
        
        for phrase in phrases:
            char_count = len(phrase.replace(" ", ""))
            phrase_duration = (char_count / total_chars) * duration if total_chars > 0 else duration / len(phrases)
            
            # メモリ節約のためサイズとフォントサイズを縮小 (480x854基準)
            txt_clip = TextClip(
                phrase, fontsize=35, color='white', stroke_color='black', stroke_width=1,
                method='caption', size=(400, None), align='center', font=selected_font
            )
            w, h = txt_clip.size
            bg_box = ColorClip(size=(w + 30, h + 15), color=(0,0,0)).set_opacity(0.6)
            # 縦方向の位置を調整
            bg_box = bg_box.set_position(('center', 600 - 10)).set_start(current_time).set_duration(phrase_duration)
            txt_clip = txt_clip.set_position(('center', 600)).set_start(current_time).set_duration(phrase_duration)
            text_clips.extend([bg_box, txt_clip])
            current_time += phrase_duration
            
        if not is_pro:
            # 無料版：透かしをより確実に表示
            wm_clip = TextClip(
                "Powered by SnappVid (Free Plan)", 
                fontsize=24, 
                color='white', 
                font=selected_font,
                stroke_color='black',
                stroke_width=1
            ).set_position(('center', 780)).set_duration(duration).set_opacity(0.6)
            text_clips.append(wm_clip)
        
        print(f"[{job_id}] STEP 4: Starting video synthesis...")
        update_job_db(job_id, message="Synthesizing final video (480p)...")
        
        # 背景画像の設定
        bg_clip = ImageClip(bg_image_path).set_duration(duration)
        
        if is_pro:
            # Proユーザーには滑らかなズームアニメーション（Ken Burns）を追加
            # 負荷を抑えるため倍率を控えめ（10%増）にする
            bg_clip = bg_clip.resize(lambda t: 1.0 + 0.1 * (t / duration))
            print(f"[{job_id}] Zoom effect applied for Pro user.")
            
        bg_clip = bg_clip.resize(height=854).set_position(('center', 'center'))
        
        final_video = CompositeVideoClip([bg_clip] + text_clips, size=(480, 854))
        final_video = final_video.set_audio(audio_clip)
        
        print(f"[{job_id}] Exporting video file (24fps, single-thread)...")
        # 24fps & シングルスレッドで極限まで負荷を軽減
        final_video.write_videofile(
            output_mp4_path,
            fps=24,
            codec="libx264",
            audio_codec="aac",
            preset="ultrafast",
            logger=None,
            threads=1
        )
        
        print(f"[{job_id}] Video synthesis complete: {output_mp4_path}")
        
        audio_clip.close()
        final_video.close()
        bg_clip.close()

        # 一時ファイルの削除
        if os.path.exists(audio_path):
            os.remove(audio_path)
            print(f"[{job_id}] Removed temporary file: {audio_path}")
        if os.path.exists(bg_image_path):
            os.remove(bg_image_path)
            print(f"[{job_id}] Removed temporary file: {bg_image_path}")

        # ==== クレジット消費 (成功時のみ) ====
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        if is_special_trial:
            cursor.execute("UPDATE users SET plan_status = 'Free', credits = -1 WHERE email = ?", (email,))
        else:
            cursor.execute("UPDATE users SET credits = credits - 10 WHERE email = ?", (email,))
        
        # ユーザーの最終動画URLも保存
        cursor.execute("UPDATE users SET last_video_url = ? WHERE email = ?", (f"/files/{video_filename}", email))
        
        conn.commit()
        conn.close()

        stop_pseudo_progress(100)
        update_job_db(job_id, 
                      status="completed", 
                      message="Completed!", 
                      video_url=f"/files/{video_filename}")
        
        if job_id in job_status:
            job_status[job_id]["local_path"] = output_mp4_path

    except Exception as e:
        if 'stop_event' in locals():
            stop_event.set()
        if 'progress_thread' in locals() and progress_thread and progress_thread.is_alive():
            progress_thread.join()
            
        job_id_safe = job_id if 'job_id' in locals() else 'unknown'
        print(f"[{job_id_safe}] Error: {e}")
                
        update_job_db(job_id, status="error", error=str(e))
    finally:
        active_generation_users.discard(email)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
