import os
import subprocess
import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, BackgroundTasks

app = FastAPI(title="XVIO WebP Trailer Engine")

R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_KEY = os.getenv("R2_KEY")
R2_SECRET = os.getenv("R2_SECRET")
BUCKET_NAME = os.getenv("R2_BUCKET", "xvio-previews")
PUBLIC_DOMAIN = os.getenv("R2_PUBLIC_DOMAIN")

s3 = None
if R2_ENDPOINT and R2_KEY and R2_SECRET:
    s3 = boto3.client(
        's3',
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_KEY,
        aws_secret_access_key=R2_SECRET
    )

def create_and_upload_webp(media_key: str, yt_id: str):
    if not s3:
        print("[HATA] S3 R2 kimlik bilgileri tanımlı değil!")
        return

    output_webp = f"{media_key}.webp"
    s3_key = f"previews/{output_webp}"

    cmd = (
        f'STREAM_URL=$(yt-dlp --extractor-args "youtube:player_client=android,web" -g -f "18/best[ext=mp4]/best" "https://www.youtube.com/watch?v={yt_id}") && '
        f'ffmpeg -ss 00:00:15 -t 6 -i "$STREAM_URL" -vf "fps=14,scale=480:-1:flags=lanczos" '
        f'-vcodec libwebp -lossless 0 -compression_level 4 -q:v 50 -loop 0 "{output_webp}" -y'
    )
    
    try:
        res = subprocess.run(cmd, shell=True, timeout=90, capture_output=True)
        if res.returncode == 0 and os.path.exists(output_webp):
            s3.upload_file(
                output_webp,
                BUCKET_NAME,
                s3_key,
                ExtraArgs={'ContentType': 'image/webp', 'CacheControl': 'public, max-age=31536000'}
            )
            print(f"[OK] R2'ye yüklendi: {s3_key}")
        else:
            err = res.stderr.decode('utf-8', errors='ignore') if res.stderr else "Bilinmeyen hata"
            print(f"[HATA] ffmpeg/yt-dlp hatası ({media_key}): {err[-250:]}")
    except Exception as e:
        print(f"[HATA] WebP dönüşüm hatası ({media_key}): {str(e)}")
    finally:
        if os.path.exists(output_webp):
            try:
                os.remove(output_webp)
            except Exception:
                pass

@app.get("/")
def health():
    return {"status": "ok", "service": "xvio-preview-bot"}

@app.get("/trigger")
def trigger_preview(media_key: str, yt_id: str, background_tasks: BackgroundTasks):
    s3_key = f"previews/{media_key}.webp"
    
    if s3:
        try:
            s3.head_object(Bucket=BUCKET_NAME, Key=s3_key)
            return {"status": "ready", "url": f"{PUBLIC_DOMAIN}/{s3_key}"}
        except ClientError:
            pass

    background_tasks.add_task(create_and_upload_webp, media_key, yt_id)
    return {"status": "processing", "url": f"{PUBLIC_DOMAIN}/{s3_key}"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
