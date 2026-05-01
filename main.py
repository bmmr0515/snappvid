import os
import uuid
import sqlite3
import time
import glob
from typing import Optional

from fastapi import FastAPI, HTTPException, Form, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from dotenv import load_dotenv
from openai import OpenAI
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
import bcrypt

# .envファイルから環境変数を読み込む
load_dotenv()

# ==== App Setup ====
app = FastAPI(
    title="Faceless Video Generator SaaS API",
    description="TikTok/Shorts向けの顔出しなし動画を全自動生成するSaaS向けAPI",
    version="2.0.0"
)

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
    
    cursor.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'credits' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN credits INTEGER DEFAULT 1")
    if 'is_verified' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 0")
        cursor.execute("ALTER TABLE users ADD COLUMN otp_code TEXT")
        
    # 新規：ゲストユーザーの生成履歴
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS guest_generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT NOT NULL,
            user_agent TEXT NOT NULL,
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

# Clients Initialize
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
if STRIPE_API_KEY:
    stripe.api_key = STRIPE_API_KEY

def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
    return hashed.decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

# ==== Models ====
class VideoRequest(BaseModel):
    theme: str = Field(..., max_length=100, description="The topic for the video (max 100 chars)")
    email: str

class VideoResponse(BaseModel):
    status: str
    message: str
    job_id: str
    theme: str
    script: str
    video_url: str
    local_path: str

class AuthResponse(BaseModel):
    status: str
    message: str
    user_id: Optional[str] = None
    email: Optional[str] = None
    plan_status: Optional[str] = None
    credits: Optional[int] = None

# ==== Auth Endpoints ====
@app.post("/signup", response_model=AuthResponse)
@limiter.limit("5/minute")
async def signup(request: Request, email: str = Form(...), password: str = Form(...)):
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Invalid email format.")
        
    domain = email.split('@')[-1].lower()
    if domain in DISPOSABLE_DOMAINS:
        raise HTTPException(status_code=400, detail="Disposable email addresses are not allowed.")
        
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Check if email exists
    cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Email already registered")
        
    user_id = str(uuid.uuid4())
    password_hash = hash_password(password)
    
    try:
        cursor.execute(
            "INSERT INTO users (id, email, password_hash, plan_status, credits) VALUES (?, ?, ?, ?, ?)",
            (user_id, email, password_hash, "Free", 10)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
        
    return AuthResponse(
        status="success", 
        message="Registration successful.", 
        user_id=user_id, 
        email=email, 
        plan_status="Free",
        credits=10
    )

@app.post("/login", response_model=AuthResponse)
@limiter.limit("10/minute")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, email, password_hash, plan_status, credits FROM users WHERE email = ?", (email,))
    user = cursor.fetchone()
    conn.close()
    
    if not user or not verify_password(password, user[2]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
        
    return AuthResponse(
        status="success", 
        message="Login successful", 
        user_id=user[0], 
        email=user[1], 
        plan_status=user[3],
        credits=user[4]
    )

# ==== Stripe Endpoints ====
@app.post("/create-checkout-session")
async def create_checkout_session(email: str = Form(...), plan_type: str = Form(...)):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe API key not configured")
        
    price_id = None
    if plan_type == "Starter":
        price_id = STRIPE_PRICE_ID_STARTER
    elif plan_type == "Pro":
        price_id = STRIPE_PRICE_ID_PRO_ANNUAL
    elif plan_type == "Top-up":
        price_id = STRIPE_PRICE_ID_TOPUP
    else:
        raise HTTPException(status_code=400, detail="Invalid plan type")
        
    if not price_id:
        raise HTTPException(status_code=500, detail=f"Price ID for {plan_type} is not configured.")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }],
            mode='payment' if plan_type == "Top-up" else 'subscription',
            success_url='http://localhost:8000/success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url='http://localhost:8000/cancel',
            customer_email=email,
            client_reference_id=email, # DB更新のキーとしてemailを使用
            metadata={
                "type": plan_type,
                "user_id": email
            }
        )
        return {"checkout_url": session.url}
    except stripe.error.StripeError as e:
        import traceback
        print("=== Stripe API Error ===")
        print(f"HTTP Status: {e.http_status}")
        print(f"Code: {e.code}")
        print(f"Param: {e.param}")
        print(f"Message: {e.user_message}")
        print("Full Traceback:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Stripe Error: {e.user_message or str(e)}")
    except Exception as e:
        import traceback
        print("=== Unexpected Checkout Error ===")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

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
        metadata = session.get('metadata', {})
        plan_type = metadata.get('type')
        email = session.get('client_reference_id') or metadata.get('user_id')
        
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

    return {"status": "success"}

# ==== Video Generation Endpoint ====
@app.post("/generate-video", response_model=VideoResponse)
@limiter.limit("3/minute")
async def generate_video(request: Request, body: VideoRequest):
    if not openai_client or not ELEVENLABS_API_KEY:
        raise HTTPException(status_code=500, detail="API Keys are not configured properly.")

    theme = sanitize_theme(body.theme)
    email = body.email
    
    if email in active_generation_users:
        raise HTTPException(status_code=429, detail="A video generation is already in progress for this account.")

    cleanup_old_files()

    # ==== クレジット確認 ====
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, plan_status, credits FROM users WHERE email = ?", (email,))
    user = cursor.fetchone()
    
    if not user:
        conn.close()
        raise HTTPException(status_code=401, detail="User not found. Please login.")
        
    user_id, plan_status, credits = user[0], user[1], user[2]
    is_special_trial = False
    
    if credits < 10:
        # moriretsu06@gmail.com の場合、特別に許可する
        if email == "moriretsu06@gmail.com":
            is_special_trial = True
            print(f"Special Pro Trial granted for {email}")
        else:
            conn.close()
            raise HTTPException(status_code=403, detail="Credit limit reached. Please purchase more credits or upgrade your plan.")
    else:
        # ==== 仮押さえ処理 ====
        cursor.execute("UPDATE users SET credits = credits - 10 WHERE email = ? AND credits >= 10", (email,))
        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=403, detail="Credit limit reached or parallel generation detected.")
        conn.commit()
    
    conn.close()
    
    is_pro = (plan_status == "Pro") or is_special_trial

    active_generation_users.add(email)
    try:
        job_id = str(uuid.uuid4())
        timestamp = int(time.time())
        video_filename = f"video_{user_id}_{timestamp}.mp4"
        
        audio_path = os.path.join("output", f"audio_{job_id}.mp3")
        bg_image_path = os.path.join("output", f"bg_{job_id}.png")
        output_mp4_path = os.path.join("output", video_filename)

        # 1. 台本生成: OpenAI API (GPT-4o)
        print(f"[{job_id}] Generating script for theme: {theme}")
        
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
            ]
        )
        script = response.choices[0].message.content
        print(f"[{job_id}] Script generated:\n{script}\n")

        # 2. 音声生成: ElevenLabs API (REST API)
        print(f"[{job_id}] Generating audio...")
        
        voice_id = "pNInz6obpgDQGcFmaJgB" # Adam
        elevenlabs_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        
        headers = {
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": ELEVENLABS_API_KEY
        }
        
        data = {
            "text": script,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.5
            }
        }
        
        response_audio = requests.post(elevenlabs_url, json=data, headers=headers)
        
        if response_audio.status_code != 200:
            raise Exception(f"ElevenLabs API Error: {response_audio.status_code} - {response_audio.text}")
            
        with open(audio_path, "wb") as f:
            f.write(response_audio.content)
            
        print(f"[{job_id}] Audio saved to {audio_path}")

        # 3. 背景画像の生成とダウンロード: DALL-E 3
        # 【コスト削減提案】
        # APIコストを無料に近づける場合、DALL-Eの代わりに Unsplash API などを利用して
        # theme に関連するフリー画像を取得する仕組みに変更することを推奨します。
        # 例: requests.get(f"https://api.unsplash.com/photos/random?query={theme}&client_id=YOUR_KEY")
        print(f"[{job_id}] Generating background image via DALL-E 3...")
        
        image_response = openai_client.images.generate(
            model="dall-e-3",
            prompt=theme,
            size="1024x1792",
            quality="standard",
            n=1,
        )
        image_url = image_response.data[0].url
        img_data = requests.get(image_url).content
        with open(bg_image_path, "wb") as f:
            f.write(img_data)
        
        # 4. 字幕(テロップ)の分割と合成
        raw_phrases = [p.strip() for p in re.split(r'(?<=[.!?]) +', script) if p.strip()]
        if not raw_phrases:
            raw_phrases = [script]
            
        # UI被りや長すぎる文章を避けるため、1フレーズ最大7単語程度に細かく分割
        phrases = []
        for rp in raw_phrases:
            words = rp.split()
            for i in range(0, len(words), 7):
                phrases.append(" ".join(words[i:i+7]))
            
        audio_clip = AudioFileClip(audio_path)
        duration = audio_clip.duration
        
        # 空白を除外した純粋な文字数で比率を計算し、音声との完璧な同期を目指す
        total_chars = sum(len(p.replace(" ", "")) for p in phrases)
        text_clips = []
        current_time = 0
        
        # Mac環境で確実に存在するフォントファイルのフルパスを探す
        font_candidates = [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Times.ttc"
        ]
        selected_font = "Arial" # 最終フォールバック
        for fp in font_candidates:
            if os.path.exists(fp):
                selected_font = fp
                break
        
        print(f"[{job_id}] Preparing subtitles using font: {selected_font}")
        for phrase in phrases:
            char_count = len(phrase.replace(" ", ""))
            phrase_duration = (char_count / total_chars) * duration
            
            # 絶対パスでフォントを指定して確実に読み込ませる
            txt_clip = TextClip(
                phrase, 
                fontsize=70, 
                color='white', 
                stroke_color='black', 
                stroke_width=2,
                method='caption',
                size=(900, None),
                align='center',
                font=selected_font
            )
            
            # 半透明の黒背景ボックスを生成して視認性を高める
            w, h = txt_clip.size
            bg_box = ColorClip(size=(w + 60, h + 40), color=(0,0,0)).set_opacity(0.6)
            
            # 画面下部から25〜30%ほど上げた位置(y=1250付近)に配置し、TikTok/ShortsのUI被りを回避
            bg_box = bg_box.set_position(('center', 1250 - 20)).set_start(current_time).set_duration(phrase_duration)
            txt_clip = txt_clip.set_position(('center', 1250)).set_start(current_time).set_duration(phrase_duration)
            
            text_clips.extend([bg_box, txt_clip])
            current_time += phrase_duration
            
        # ウォーターマーク (無料版のみ追加、Pro版や特別試用版では非表示)
        if not is_pro:
            wm_clip = TextClip(
                "Powered by SnappVid", 
                fontsize=40, 
                color='white', 
                font=selected_font
            ).set_position(('center', 1600)).set_duration(duration).set_opacity(0.4)
            text_clips.append(wm_clip)
            
        # 5. 動画合成: MoviePy
        print(f"[{job_id}] Synthesizing final video...")
        
        bg_clip = ImageClip(bg_image_path).set_duration(duration)
        
        # Ken Burns 効果（ゆっくりズームイン）
        # Pro版の場合はよりダイナミックにズーム
        zoom_factor = 0.15 if is_pro else 0.08
        def zoom_in(t):
            return 1.0 + zoom_factor * (t / duration)
            
        bg_clip = bg_clip.resize(zoom_in).set_position(('center', 'center'))
        
        final_video = CompositeVideoClip([bg_clip] + text_clips, size=(1024, 1792))
        final_video = final_video.set_audio(audio_clip)
        
        final_video.write_videofile(
            output_mp4_path,
            fps=30,
            codec="libx264",
            audio_codec="aac",
            preset="ultrafast",
            logger=None
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

        # ==== クレジット消費 ====
        # 特別試用版の場合は、強制的にクレジットを -1 (以降ブロック) にする
        if is_special_trial:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET plan_status = 'Free', credits = -1 WHERE email = ?", (email,))
            conn.commit()
            conn.close()
        # 通常ユーザーは開始時に仮押さえ済みのため、成功時は何もしない

        return VideoResponse(
            status="success",
            message="Video generated successfully.",
            job_id=job_id,
            theme=theme,
            script=script,
            video_url=f"/files/{video_filename}",
            local_path=output_mp4_path
        )

    except Exception as e:
        job_id_safe = job_id if 'job_id' in locals() else 'unknown'
        print(f"[{job_id_safe}] Error: {e}")
        
        # ==== 仮押さえの返却 ====
        if not is_special_trial:
            try:
                conn_err = sqlite3.connect(DB_FILE)
                cursor_err = conn_err.cursor()
                cursor_err.execute("UPDATE users SET credits = credits + 10 WHERE email = ?", (email,))
                conn_err.commit()
                conn_err.close()
                print(f"[{job_id_safe}] Refunded 10 credits to {email}")
            except Exception as refund_err:
                print(f"Failed to refund credits: {refund_err}")
                
        try:
            conn.close()
        except:
            pass
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        active_generation_users.discard(email)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
