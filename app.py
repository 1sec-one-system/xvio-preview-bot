import os
import json
import subprocess
import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, BackgroundTasks

app = FastAPI(title="XVIO WebP Trailer Engine - Most Replayed AI")

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

def find_best_scene_times(yt_id: str) -> tuple[int, int]:
    """
    YouTube'un 'Most Replayed' (Isı Haritası) verisini analiz eder.
    Milyonlarca insanın en çok tekrar izlediği zirve 9-10 saniyeyi bulur.
    Heatmap yoksa videonun en aksiyonlu %45'lik dilimini seçer.
    """
    cmd = ['yt-dlp', '--dump-json', '--no-playlist', f'https://www.youtube.com/watch?v={yt_id}']
    try:
        res = subprocess.run(cmd, timeout=30, capture_output=True, text=True)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            heatmap = data.get('heatmap')
            duration = int(data.get('duration') or 120)
            
            if heatmap and len(heatmap) > 0:
                # Isı haritasında en yüksek izlenme skoruna sahip dilimi bul
                peak = max(heatmap, key=lambda x: x.get('value', 0))
                peak_time = int(peak.get('start_time', 45))
                # Tepe noktasının 2 saniye öncesinden başlatıp 9 saniye al
                start = max(15, peak_time - 2)
                end = min(duration - 2, start + 9)
                print(f"[HEATMAP] {yt_id} için en popüler an bulundu: {start}. sn - {end}. sn")
                return start, end
            else:
                # Isı haritası yoksa (yeni fragman): %40-%45 dilimini al
                start = int(duration * 0.42)
                return start, start + 9
    except Exception as e:
        print(f"[HEATMAP UYARI] {e}, varsayılan süreye dönülüyor")
    
    return 30, 39

def create_and_upload_webp(media_key: str, yt_id: str):
    if not s3:
        print("[HATA] S3 R2 kimlik bilgileri tanımlı değil!")
        return

    clip_file = f"clip_{media_key}.mp4"
    output_webp = f"{media_key}.webp"
    s3_key = f"previews/{output_webp}"

    start_sec, end_sec = find_best_scene_times(yt_id)
    time_range = f"*{start_sec:02d}-{end_sec:02d}"

    # YouTube visionos/HLS akışıyla bot korumasını aş ve doğrudan en popüler sahneyi indir
    cmd_dl = [
        "yt-dlp",
        "-f", "230/229/604/605/18/best",
        "--download-sections", f"*{start_sec}-{end_sec}",
        "-o", clip_file,
        f"https://www.youtube.com/watch?v={yt_id}",
        "--force-overwrites",
        "--no-playlist"
    ]

    # Netflix kalitesinde 14 FPS, 480px, sinematik hafif WebP oluştur (~300-380 KB)
    cmd_conv = [
        "ffmpeg",
        "-i", clip_file,
        "-vf", "fps=14,scale=480:-1:flags=lanczos",
        "-vcodec", "libwebp",
        "-lossless", "0",
        "-compression_level", "4",
        "-q:v", "50",
        "-loop", "0",
        output_webp,
        "-y"
    ]

    try:
        print(f"[BASLADI] {media_key} popüler sahnesi indiriliyor ({start_sec}s - {end_sec}s)...")
        res_dl = subprocess.run(cmd_dl, timeout=50, capture_output=True)
        if res_dl.returncode != 0 or not os.path.exists(clip_file):
            err = res_dl.stderr.decode('utf-8', errors='ignore') if res_dl.stderr else "Indirme basarisiz"
            print(f"[HATA] İndirme hatası ({media_key}): {err[-200:]}")
            return

        print(f"[DONUSTURULUYOR] {media_key} WebP yapılıyor...")
        res_conv = subprocess.run(cmd_conv, timeout=35, capture_output=True)
        if res_conv.returncode == 0 and os.path.exists(output_webp):
            s3.upload_file(
                output_webp,
                BUCKET_NAME,
                s3_key,
                ExtraArgs={'ContentType': 'image/webp', 'CacheControl': 'public, max-age=31536000'}
            )
            print(f"[BASARILI] Netflix kalitesinde R2'ye yüklendi: {s3_key}")
        else:
            err = res_conv.stderr.decode('utf-8', errors='ignore') if res_conv.stderr else "Donusturme basarisiz"
            print(f"[HATA] Dönüştürme hatası ({media_key}): {err[-200:]}")
    except Exception as e:
        print(f"[BEKLENMEYEN HATA] {media_key}: {str(e)}")
    finally:
        for f in (clip_file, output_webp):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

@app.get("/")
def health():
    return {"status": "ok", "service": "xvio-preview-bot-most-replayed"}

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
