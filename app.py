import os
import sys
import re
import time
import hmac
import json
import asyncio
import subprocess
from typing import Optional, List, Dict, Any
import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Request, HTTPException, Query
from fastapi.responses import RedirectResponse, JSONResponse
import httpx

WIN_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

app = FastAPI(title="XVIO Smart Stream Engine & Cloud Gateway")

# -------------------------------------------------------------
# ⚙️ YAPILANDIRMA VE ORTAM DEĞİŞKENLERİ
# -------------------------------------------------------------
TORBOX_API_KEY = os.getenv("TORBOX_API_KEY", "9d86c44f-c2b8-4b03-b309-38464a3422ec").strip()
XVIO_GATEWAY_TOKEN = os.getenv("XVIO_GATEWAY_TOKEN", "").strip()

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

# -------------------------------------------------------------
# 🛡️ GÜVENLİK VE ERİŞİM DENETİMİ
# -------------------------------------------------------------
def verify_access_token(request: Request, token: Optional[str] = None) -> bool:
    """
    Eğer sunucuda XVIO_GATEWAY_TOKEN tanımlıysa zorunlu kılar.
    Tanımlı değilse genel erişime izin verir.
    """
    if not XVIO_GATEWAY_TOKEN:
        return True

    req_token = request.headers.get("X-XVIO-TOKEN")
    if req_token and hmac.compare_digest(req_token.strip(), XVIO_GATEWAY_TOKEN):
        return True

    url_token = token or request.query_params.get("token")
    if url_token and hmac.compare_digest(url_token.strip(), XVIO_GATEWAY_TOKEN):
        return True

    raise HTTPException(status_code=403, detail="Yetkisiz Erişim: Geçersiz veya eksik XVIO Gateway Güvenlik Anahtarı.")

system_logs: List[str] = []

def log_msg(msg: str):
    print(msg, flush=True)
    system_logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    if len(system_logs) > 100:
        system_logs.pop(0)

# -------------------------------------------------------------
# 💾 15 DAKİKALIK RAM ÖNBELLEĞİ (IN-MEMORY STREAM CACHE)
# -------------------------------------------------------------
STREAM_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 900  # 15 Dakika

def get_cached_streams(cache_key: str) -> Optional[Dict[str, Any]]:
    entry = STREAM_CACHE.get(cache_key)
    if entry:
        if time.time() < entry["expires_at"]:
            return entry["data"]
        else:
            del STREAM_CACHE[cache_key]
    return None

def set_cached_streams(cache_key: str, data: Dict[str, Any]):
    # Eski kayıtları süpür (max 300 anahtar)
    if len(STREAM_CACHE) > 300:
        now = time.time()
        expired = [k for k, v in STREAM_CACHE.items() if now >= v["expires_at"]]
        for k in expired:
            STREAM_CACHE.pop(k, None)

    STREAM_CACHE[cache_key] = {
        "expires_at": time.time() + CACHE_TTL_SECONDS,
        "data": data
    }

# -------------------------------------------------------------
# 🎯 AKILLI SIRALAMA, PUANLAMA VE FİLTRELEME MOTORU
# -------------------------------------------------------------
QUALITY_REGEX = re.compile(r'\b(2160p|4k|1080p|720p|480p)\b', re.IGNORECASE)
TURKISH_REGEX = re.compile(r'\b(turkce|turkish|tr|dual|multi)\b', re.IGNORECASE)
FOREIGN_DUB_REGEX = re.compile(r'\b(castellano|español|ita|italian|french|vostfr|rus|russian|hindi|latino)\b', re.IGNORECASE)
SIZE_REGEX = re.compile(r'([0-9]+(?:\.[0-9]+)?)\s*(GB|MB)', re.IGNORECASE)

def parse_size_gb(title: str, name: str) -> float:
    text = f"{name} {title}"
    match = SIZE_REGEX.search(text)
    if match:
        val = float(match.group(1))
        unit = match.group(2).upper()
        if unit == "GB":
            return val
        elif unit == "MB":
            return val / 1024.0
    return 0.0

def score_stream(stream: Dict[str, Any], target_season: Optional[int] = None, target_episode: Optional[int] = None) -> float:
    score = 1000.0
    name = stream.get("name", "")
    title = stream.get("title", "")
    text = f"{name} {title}".lower()

    # 1. Torbox Instant Cache Ödülü (En Yüksek Öncelik)
    is_cached = "[tb+]" in text or "cached" in text or stream.get("behaviorHints", {}).get("cached", False)
    if is_cached:
        score += 5000.0
    elif "[tb" in text:
        score += 1000.0

    # 2. Çözünürlük Puanı
    if "2160p" in text or "4k" in text:
        score += 800.0
    elif "1080p" in text:
        score += 600.0
    elif "720p" in text:
        score += 300.0
    elif "480p" in text or "cam" in text:
        score -= 500.0

    # 3. Türkçe Dublaj / Altyazı Önceliği
    if TURKISH_REGEX.search(text):
        score += 450.0

    # 4. Yabancı Bölgesel Dublaj Cezası (Türkçe yokken)
    if FOREIGN_DUB_REGEX.search(text) and not TURKISH_REGEX.search(text):
        score -= 600.0

    # 5. Dosya Boyutu Dengesi (Çok aşırı hantal olanları hafifçe törpüle)
    size_gb = parse_size_gb(title, name)
    if 2.0 <= size_gb <= 18.0:
        score += 200.0  # İdeal boyut
    elif size_gb > 45.0:
        score -= 300.0  # TV Stick'i yorabilecek remux'lar

    # 6. Dizi İçin Yanlış Sezon/Bölüm Cezası
    if target_season is not None and target_episode is not None:
        target_s_str = f"s{target_season:02d}"
        target_e_str = f"e{target_episode:02d}"
        if target_s_str in text and target_e_str not in text:
            score -= 4000.0  # Aynı sezon ama başka bölüm!

    return score

# -------------------------------------------------------------
# ⚡ PARALEL KAZIYICI MOTORU (TORBOX + TORRENTIO + COMET)
# -------------------------------------------------------------
async def fetch_torrentio_torbox(media_type: str, stream_id: str, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    if not TORBOX_API_KEY:
        return []
    url = f"https://torrentio.strem.fun/torbox={TORBOX_API_KEY}/stream/{media_type}/{stream_id}.json"
    try:
        resp = await client.get(url, timeout=6.0)
        if resp.status_code == 200:
            data = resp.json()
            streams = data.get("streams", [])
            for s in streams:
                s["addon_source"] = "torrentio_torbox"
            return streams
    except Exception as e:
        log_msg(f"[TORRENTIO HATA] {media_type}/{stream_id}: {e}")
    return []

async def fetch_comet_torbox(media_type: str, stream_id: str, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    # Comet debrid yedek kaynağı
    try:
        # Comet açık Stremio endpoint formatı
        url = f"https://comet.elfhosted.com/stremio/torbox={TORBOX_API_KEY}/stream/{media_type}/{stream_id}.json"
        resp = await client.get(url, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            streams = data.get("streams", [])
            for s in streams:
                s["addon_source"] = "comet_torbox"
            return streams
    except Exception:
        pass
    return []

# -------------------------------------------------------------
# 🌐 AKILLI STREAM GATEWAY ENDPOINTS
# -------------------------------------------------------------
@app.get("/api/v1/streams/{media_type}/{imdb_id}")
async def resolve_streams(
    media_type: str,
    imdb_id: str,
    request: Request,
    season: Optional[int] = Query(None),
    episode: Optional[int] = Query(None),
    token: Optional[str] = Query(None)
):
    """
    ⚡ XVIO SMART STREAM GATEWAY:
    1. 15 dakikalık RAM önbelleğinden 1 ms'de yanıt döner.
    2. Önbellekte yoksa Torrentio ve Comet üzerinden Torbox akışlarını paralel çeker.
    3. Torbox bulutunda hazır (cached) olanları en tepeye dizer, Türkçe seslilere bonus verir.
    4. Cihazı yormayacak, temiz ve sıralı en kaliteli 15 akışı döndürür.
    """
    verify_access_token(request, token)

    # Dizi formatı normalizasyonu (tt0903747:1:1 veya query param)
    stream_id = imdb_id
    target_season = season if isinstance(season, int) else None
    target_episode = episode if isinstance(episode, int) else None

    if ":" in imdb_id:
        parts = imdb_id.split(":")
        stream_id = imdb_id
        if len(parts) >= 3:
            try:
                target_season = int(parts[1])
                target_episode = int(parts[2])
            except ValueError:
                pass
    elif media_type == "series" and target_season is not None and target_episode is not None:
        stream_id = f"{imdb_id}:{target_season}:{target_episode}"

    cache_key = f"{media_type}:{stream_id}"
    cached_result = get_cached_streams(cache_key)
    if cached_result:
        log_msg(f"[CACHE HIT] {cache_key} - 0ms önbellekten sunuldu")
        return cached_result

    start_time = time.time()

    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0 (XVIO-Gateway/2.0)"}) as client:
        results = await asyncio.gather(
            fetch_torrentio_torbox(media_type, stream_id, client),
            fetch_comet_torbox(media_type, stream_id, client),
            return_exceptions=True
        )

    all_streams: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, list):
            all_streams.extend(r)

    if not all_streams:
        log_msg(f"[UYARI] {cache_key} için akış bulunamadı ({time.time() - start_time:.2f}s)")
        return {"streams": [], "cached": False, "count": 0, "media": cache_key}

    # Puanla ve sırala
    scored_streams = []
    for s in all_streams:
        sc = score_stream(s, target_season, target_episode)
        scored_streams.append((sc, s))

    # En yüksek puanlıdan en düşüğe
    scored_streams.sort(key=lambda x: x[0], reverse=True)

    # En iyi 15 akışı seç
    top_streams = [s for _, s in scored_streams[:15]]

    elapsed_ms = int((time.time() - start_time) * 1000)
    has_cached = any("[tb+]" in (s.get("name", "") + s.get("title", "")).lower() for s in top_streams)

    response_data = {
        "cached": has_cached,
        "count": len(top_streams),
        "total_scraped": len(all_streams),
        "elapsed_ms": elapsed_ms,
        "gateway": "xvio_smart_gateway",
        "streams": top_streams
    }

    set_cached_streams(cache_key, response_data)
    log_msg(f"[ÇÖZÜLDÜ] {cache_key} -> {len(top_streams)} akış ({elapsed_ms}ms, cached={has_cached})")
    return response_data

@app.get("/streams/{media_type}/{stream_id}.json")
async def stremio_manifest_stream(media_type: str, stream_id: str, request: Request, token: Optional[str] = Query(None)):
    """Stremio standardında doğrudan eklenti formatı sunar."""
    res = await resolve_streams(media_type, stream_id, request, token=token)
    return {"streams": res.get("streams", [])}

@app.get("/api/v1/torbox/status")
async def get_torbox_status(request: Request, token: Optional[str] = Query(None)):
    """Torbox hesap ve plan durumunu doğrular."""
    verify_access_token(request, token)
    if not TORBOX_API_KEY:
        return {"status": "error", "message": "TORBOX_API_KEY tanimli degil"}

    url = "https://api.torbox.app/v1/api/user/me"
    try:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {TORBOX_API_KEY}", "User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url, timeout=6.0)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                return {
                    "status": "ok",
                    "plan": data.get("plan"),
                    "email": data.get("email"),
                    "total_downloaded_gb": round(data.get("total_downloaded", 0) / (1024**3), 2),
                    "active_torrents": data.get("active_torrents", 0)
                }
            return {"status": "error", "code": resp.status_code, "detail": resp.text}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# -------------------------------------------------------------
# 🎬 MEVCUT R2 WEBP & SAĞLIK DENETİMİ ENDPOINTLERİ
# -------------------------------------------------------------
@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "xvio-smart-stream-engine",
        "torbox_ready": bool(TORBOX_API_KEY),
        "gateway_security": bool(XVIO_GATEWAY_TOKEN),
        "cache_entries": len(STREAM_CACHE)
    }

@app.get("/logs")
def get_logs(request: Request, token: Optional[str] = None):
    verify_access_token(request, token)
    return {"status": "ok", "logs": system_logs}

@app.get("/video/{media_key}.webp")
def resolve_preview_video(media_key: str, request: Request, token: Optional[str] = None):
    verify_access_token(request, token)
    storage_pools = ["https://pub-96c3b6c5b7b0484b93167e65c2910bba.r2.dev"]
    extra_pools = os.getenv("R2_STORAGE_POOLS", "")
    if extra_pools:
        for p in extra_pools.split(","):
            p_clean = p.strip()
            if p_clean and p_clean not in storage_pools:
                storage_pools.append(p_clean)
    target_url = f"{storage_pools[0]}/previews/{media_key}.webp"
    return RedirectResponse(url=target_url, status_code=302)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
