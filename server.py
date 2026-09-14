from flask import Flask, request, Response, jsonify, render_template_string, redirect, stream_with_context
import subprocess
import sys
import os
import json
import time
import re
import requests
from datetime import datetime, timedelta
from urllib.parse import quote
from xml.sax.saxutils import escape
from concurrent.futures import ThreadPoolExecutor, as_completed

PORT = int(os.environ.get("PORT", "9001"))
CACHE_SECONDS = 75
MAX_WORKERS = 6

app = Flask(__name__)
channel_cache = {}


def public_base_url():
    forced = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if forced:
        return forced
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}"


# =========================================================
# LIVE AD-FREE
# =========================================================

PROXY_CACHE_SECONDS = 45
proxy_cache = {}
proxy_http = requests.Session()
proxy_http.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    )
})


def clean_proxy_candidates(streamer):
    channel = quote(streamer, safe="")
    params = "?allow_source=true&allow_audio_only=true&fast_bread=true"
    encoded = (
        f"{channel}.m3u8"
        "%3Fplayer%3Dtwitchweb"
        "%26type%3Dany"
        "%26allow_source%3Dtrue"
        "%26allow_audio_only%3Dtrue"
        "%26allow_spectre%3Dfalse"
        "%26fast_bread%3Dtrue"
    )

    # L'ordine conta: prima i proxy europei, poi gli altri fallback.
    return [
        ("Luminous EU", f"https://eu.luminous.dev/live/{channel}{params}"),
        ("Luminous EU2", f"https://eu2.luminous.dev/live/{channel}{params}"),
        ("PerfProd EU", f"https://lb-eu.cdn-perfprod.com/playlist/{encoded}"),
        ("PerfProd EU2", f"https://lb-eu2.cdn-perfprod.com/playlist/{encoded}"),
        ("Luminous AS", f"https://as.luminous.dev/live/{channel}{params}"),
    ]


def get_clean_proxy(streamer, force=False):
    key = streamer.lower()
    now = time.time()
    cached = proxy_cache.get(key)
    if cached and not force and now - cached["time"] < PROXY_CACHE_SECONDS:
        return cached["result"]

    selected = None
    for name, url in clean_proxy_candidates(streamer):
        try:
            r = proxy_http.get(url, timeout=(3.0, 6.0), allow_redirects=True)
            body = r.text[:250000]
            if r.status_code != 200 or "#EXTM3U" not in body:
                continue

            lower = body.lower()
            # Se il manifest restituito è già marcato come stitched-ad,
            # non lo consideriamo un backend pulito.
            if "twitch-stitched-ad" in lower or "stitched-ad-" in lower:
                continue

            selected = {"name": name, "url": url}
            break
        except requests.RequestException:
            continue

    proxy_cache[key] = {"time": now, "result": selected}
    return selected


def safe_console(text):
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def run_ytdlp(args, timeout=35):
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "yt-dlp error")
    out = result.stdout.strip()
    if not out:
        raise RuntimeError("yt-dlp non ha restituito dati")
    return out


def run_ytdlp_json(url, extra=None, timeout=35):
    args = ["--dump-single-json", "--skip-download"]
    if extra:
        args.extend(extra)
    args.append(url)
    raw = run_ytdlp(args, timeout=timeout)
    return json.loads(raw)


def read_streamers():
    try:
        with open("streams.txt", "r", encoding="utf-8") as f:
            seen = set()
            result = []
            for line in f:
                name = line.strip()
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    result.append(name)
            return result
    except FileNotFoundError:
        return []


def allowed_streamer(streamer):
    return streamer.lower() in {s.lower() for s in read_streamers()}


def parse_date(upload_date):
    if not upload_date or len(upload_date) != 8:
        return ""
    try:
        return datetime.strptime(upload_date, "%Y%m%d").strftime("%d/%m/%Y")
    except ValueError:
        return ""


def get_live(streamer):
    info = run_ytdlp_json(
        f"https://www.twitch.tv/{streamer}",
        extra=["-f", "best"],
    )
    stream_url = info.get("url")
    if not stream_url:
        raise RuntimeError("Nessun flusso live")
    if info.get("is_live") is False and info.get("live_status") not in ("is_live", "post_live"):
        raise RuntimeError("Canale offline")
    return {
        "status": "LIVE",
        "stream_url": stream_url,
        "title": info.get("title") or f"{streamer} è in live",
        "date": "",
        "thumbnail": info.get("thumbnail") or "",
        "vod_id": "",
    }


def get_latest_vod(streamer, position=1):
    playlist_url = f"https://www.twitch.tv/{streamer}/videos?filter=archives&sort=time"
    flat = run_ytdlp_json(
        playlist_url,
        extra=["--flat-playlist", "--playlist-items", str(position)],
    )
    entries = flat.get("entries") or []
    if not entries:
        raise RuntimeError("Nessuna VOD disponibile")
    entry = entries[0]
    vod_id = str(entry.get("id") or "").lstrip("v")
    if not vod_id:
        raise RuntimeError("ID VOD non trovato")
    vod = run_ytdlp_json(
        f"https://www.twitch.tv/videos/{vod_id}",
        extra=["-f", "best"],
    )
    stream_url = vod.get("url")
    if not stream_url:
        raise RuntimeError("URL VOD non trovato")
    return {
        "status": "REPLAY",
        "stream_url": stream_url,
        "title": vod.get("title") or entry.get("title") or "Ultima live",
        "date": parse_date(vod.get("upload_date")),
        "thumbnail": vod.get("thumbnail") or entry.get("thumbnail") or "",
        "vod_id": vod_id,
    }


def resolve_channel(streamer, force=False):
    key = streamer.lower()
    now = time.time()
    cached = channel_cache.get(key)
    if cached and not force and now - cached["time"] < CACHE_SECONDS:
        return cached["data"]

    try:
        data = get_live(streamer)
    except Exception:
        try:
            data = get_latest_vod(streamer)
        except Exception:
            data = {
                "status": "OFFLINE",
                "stream_url": "",
                "title": "Nessuna live o VOD disponibile",
                "date": "",
                "thumbnail": "",
                "vod_id": "",
            }

    data = {**data, "streamer": streamer, "updated": int(now)}
    channel_cache[key] = {"time": now, "data": data}
    return data


def channel_for_api(streamer, force=False):
    info = resolve_channel(streamer, force=force)
    public = dict(info)
    public.pop("stream_url", None)
    public["play_url"] = f"/api/play/{quote(streamer)}"
    return public


@app.after_request
def common_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/")
def home():
    return render_template_string(APP_HTML, base=public_base_url())


@app.route("/api/channels")
def api_channels():
    streamers = read_streamers()
    if not streamers:
        return jsonify([])

    result = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(streamers))) as pool:
        futures = {pool.submit(channel_for_api, s, False): s for s in streamers}
        by_name = {}
        for future in as_completed(futures):
            s = futures[future]
            try:
                by_name[s.lower()] = future.result()
            except Exception:
                by_name[s.lower()] = {
                    "streamer": s,
                    "status": "OFFLINE",
                    "title": "Errore durante il controllo",
                    "date": "",
                    "thumbnail": "",
                    "vod_id": "",
                    "play_url": f"/api/play/{quote(s)}",
                }
        for s in streamers:
            result.append(by_name[s.lower()])
    return jsonify(result)


@app.route("/api/channel/<path:streamer>")
def api_channel(streamer):
    if not allowed_streamer(streamer):
        return jsonify({"error": "Canale non presente in streams.txt"}), 404
    return jsonify(channel_for_api(streamer, force=True))


@app.route("/api/play/<path:streamer>")
def api_play(streamer):
    if not allowed_streamer(streamer):
        return jsonify({"error": "Canale non presente in streams.txt"}), 404

    info = resolve_channel(streamer, force=True)
    if info["status"] == "OFFLINE" or not info.get("stream_url"):
        return jsonify({"error": "Nessuna live o VOD disponibile", "status": "OFFLINE"}), 404

    play_url = info["stream_url"]
    adfree = False
    method = "DIRECT"

    if info["status"] == "LIVE":
        clean = get_clean_proxy(streamer, force=True)
        if clean:
            play_url = clean["url"]
            adfree = True
            method = clean["name"]
            safe_console(f"[LIVE CLEAN] {streamer} -> {method}")
        else:
            # Streamlink filtra gli ad segment. Durante un break, se non esiste
            # un flusso pulito alternativo, l'immagine può fermarsi finché torna la live.
            play_url = f"{public_base_url()}/relay/{quote(streamer)}"
            adfree = True
            method = "Streamlink filtered"
            safe_console(f"[LIVE FILTERED] {streamer} -> Streamlink")

    return jsonify({
        "streamer": streamer,
        "status": info["status"],
        "title": info["title"],
        "date": info["date"],
        "thumbnail": info["thumbnail"],
        "url": play_url,
        "adfree": adfree,
        "method": method,
    })


@app.route("/twitch")
def twitch_compat():
    streamer = request.args.get("streamer", "").strip()
    if not streamer or not allowed_streamer(streamer):
        return "Streamer non valido", 404

    info = resolve_channel(streamer, force=True)
    if info["status"] == "OFFLINE" or not info.get("stream_url"):
        return "Nessuna live o VOD disponibile", 404

    # Per le VOD non serve il proxy anti-ad.
    if info["status"] != "LIVE":
        safe_console(f"[{info['status']}] {streamer}")
        manifest = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=8000000\n"
            f"{info['stream_url']}\n"
        )
        return Response(manifest, mimetype="application/vnd.apple.mpegurl")

    # LIVE: prova prima più backend che restituiscono una playlist pulita.
    clean = get_clean_proxy(streamer, force=True)
    if clean:
        safe_console(f"[LIVE CLEAN] {streamer} -> {clean['name']}")
        return redirect(clean["url"], code=302)

    # Ultimo paracadute: Streamlink filtra gli ad segment. Non mostra lo spot,
    # ma può esserci un freeze durante il commercial break.
    safe_console(f"[LIVE FILTERED] {streamer} -> Streamlink")
    return redirect(f"{public_base_url()}/relay/{quote(streamer)}", code=302)


@app.route("/relay/<path:streamer>")
def relay(streamer):
    if not allowed_streamer(streamer):
        return "Streamer non valido", 404

    twitch_url = f"https://www.twitch.tv/{streamer}"
    command = [
        sys.executable,
        "-m",
        "streamlink",
        "--stdout",
        "--loglevel", "warning",
        "--retry-open", "2",
        "--stream-segment-attempts", "3",
        "--stream-segment-timeout", "10",
        twitch_url,
        "best",
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    def generate():
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    return Response(
        stream_with_context(generate()),
        content_type="video/mp4",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.route("/playlist.m3u")
def playlist():
    base = public_base_url()
    epg_url = f"{base}/epg.xml"
    lines = [f'#EXTM3U x-tvg-url="{epg_url}" url-tvg="{epg_url}"']
    for streamer in read_streamers():
        channel_id = streamer.lower()
        lines.append(
            f'#EXTINF:-1 tvg-id="{channel_id}" tvg-name="{streamer}" '
            f'group-title="Twitch IPTV",{streamer}'
        )
        lines.append(f"{base}/twitch?streamer={quote(streamer)}")
    return Response("\n".join(lines) + "\n", content_type="application/vnd.apple.mpegurl; charset=utf-8")


@app.route("/epg.xml")
def epg():
    now = datetime.now().astimezone()
    start = now.replace(minute=0, second=0, microsecond=0)
    stop = start + timedelta(hours=6)

    def xmltime(dt):
        return dt.strftime("%Y%m%d%H%M%S %z")

    streamers = read_streamers()
    channels_xml = []
    programmes_xml = []
    for streamer in streamers:
        cid = streamer.lower()
        info = resolve_channel(streamer)
        channels_xml.append(
            f'<channel id="{escape(cid)}"><display-name>{escape(streamer)}</display-name></channel>'
        )
        if info["status"] == "LIVE":
            title = "LIVE"
            desc = info["title"]
        elif info["status"] == "REPLAY":
            title = f"REPLAY - LIVE DEL {info['date']}" if info["date"] else "REPLAY"
            desc = info["title"]
        else:
            title = "OFFLINE"
            desc = "Nessuna live o VOD disponibile"
        programmes_xml.append(
            f'<programme start="{xmltime(start)}" stop="{xmltime(stop)}" channel="{escape(cid)}">'
            f'<title lang="it">{escape(title)}</title>'
            f'<desc lang="it">{escape(desc)}</desc>'
            f'</programme>'
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<tv generator-info-name="Cristian Twitch IPTV">\n'
        + "\n".join(channels_xml)
        + "\n"
        + "\n".join(programmes_xml)
        + "\n</tv>\n"
    )
    return Response(xml, content_type="application/xml; charset=utf-8")


@app.route("/health")
def health():
    return jsonify({"ok": True, "base": public_base_url(), "port": PORT, "channels": len(read_streamers())})


APP_HTML = r'''<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Twitch IPTV</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<style>
:root{--bg:#0e0e10;--panel:#18181b;--panel2:#1f1f23;--muted:#adadb8;--text:#efeff1;--purple:#9147ff;--purple2:#772ce8;--red:#e91916;--line:#2f2f35;--green:#00a884}
*{box-sizing:border-box}html,body{height:100%;margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;overflow:hidden}
button,input{font:inherit}.app{display:grid;grid-template-columns:290px 1fr;height:100vh}.sidebar{background:#18181b;border-right:1px solid #26262c;display:flex;flex-direction:column;min-width:0}.brand{height:68px;display:flex;align-items:center;gap:12px;padding:0 18px;border-bottom:1px solid #26262c}.brandMark{width:34px;height:34px;border-radius:10px;background:linear-gradient(145deg,var(--purple),#bf94ff);display:grid;place-items:center;font-weight:900;color:white;box-shadow:0 8px 30px #9147ff33}.brandTitle{font-weight:800;letter-spacing:.2px}.brandSub{font-size:11px;color:var(--muted);margin-top:2px}.sideSearch{padding:14px 14px 8px}.searchBox{display:flex;align-items:center;gap:8px;background:#0e0e10;border:1px solid #303039;border-radius:10px;padding:10px 11px}.searchBox input{width:100%;background:transparent;border:0;outline:0;color:white}.channelList{overflow:auto;padding:7px 8px 18px}.channelRow{width:100%;display:grid;grid-template-columns:42px 1fr auto;gap:10px;align-items:center;padding:9px 10px;border:0;border-radius:10px;background:transparent;color:white;text-align:left;cursor:pointer;transition:.15s}.channelRow:hover,.channelRow.active{background:#26262c}.avatar{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;font-weight:800;background:linear-gradient(145deg,#5c16c5,#bf94ff);overflow:hidden;border:2px solid transparent}.avatar img{width:100%;height:100%;object-fit:cover}.rowName{font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.rowTitle{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:3px}.badge{font-size:10px;font-weight:800;letter-spacing:.45px;padding:4px 6px;border-radius:5px}.badge.live{background:var(--red);color:white}.badge.replay{background:#5c16c5;color:#fff}.badge.offline{background:#323239;color:#aaa}.sideBottom{margin-top:auto;padding:12px 14px;border-top:1px solid #26262c;font-size:12px;color:var(--muted)}
.main{min-width:0;display:flex;flex-direction:column;background:#0e0e10}.topbar{height:68px;display:flex;align-items:center;justify-content:space-between;padding:0 22px;border-bottom:1px solid #26262c;background:#0e0e10dd;backdrop-filter:blur(14px)}.topTitle{font-weight:750}.topActions{display:flex;gap:8px}.btn{border:0;border-radius:8px;padding:9px 12px;background:#2f2f35;color:white;font-weight:650;cursor:pointer}.btn:hover{background:#3a3a41}.btn.primary{background:var(--purple)}.btn.primary:hover{background:var(--purple2)}
.content{overflow:auto;padding:22px;display:grid;gap:20px}.hero{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:18px}.playerShell{background:black;border-radius:14px;overflow:hidden;box-shadow:0 20px 60px #0008;border:1px solid #232329;position:relative;aspect-ratio:16/9}.playerShell video{width:100%;height:100%;display:block;background:#000}.playerEmpty{position:absolute;inset:0;display:grid;place-items:center;background:radial-gradient(circle at 50% 25%,#24113f,#09090b 55%);text-align:center;padding:30px}.playerEmpty.hidden{display:none}.emptyLogo{width:70px;height:70px;margin:auto;border-radius:20px;background:linear-gradient(145deg,var(--purple),#bf94ff);display:grid;place-items:center;font-size:26px;font-weight:900;box-shadow:0 14px 44px #9147ff44}.emptyTitle{font-size:20px;font-weight:800;margin-top:16px}.emptySub{color:var(--muted);margin-top:7px;font-size:14px}.liveCorner{position:absolute;top:12px;left:12px;display:none;gap:7px;align-items:center}.liveCorner.show{display:flex}.miniBadge{font-size:11px;font-weight:850;padding:5px 7px;border-radius:5px;background:#5c16c5}.miniBadge.live{background:var(--red)}
.infoCard{background:var(--panel);border:1px solid #26262c;border-radius:14px;padding:18px;display:flex;flex-direction:column;min-height:0}.channelHeader{display:flex;gap:12px;align-items:center}.bigAvatar{width:54px;height:54px;border-radius:50%;display:grid;place-items:center;font-size:18px;font-weight:850;background:linear-gradient(145deg,#5c16c5,#bf94ff);overflow:hidden}.bigAvatar img{width:100%;height:100%;object-fit:cover}.channelName{font-size:18px;font-weight:800}.statusText{font-size:12px;color:var(--muted);margin-top:4px}.programTitle{font-size:16px;font-weight:750;line-height:1.35;margin-top:20px}.meta{margin-top:10px;color:var(--muted);font-size:13px;line-height:1.6}.infoSpacer{flex:1}.quickGrid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:18px}.quick{background:#242429;border:1px solid #303037;border-radius:9px;padding:10px;cursor:pointer;color:white;text-align:left}.quick span{display:block;color:var(--muted);font-size:11px;margin-top:3px}
.sectionTitle{display:flex;align-items:end;justify-content:space-between}.sectionTitle h2{font-size:18px;margin:0}.sectionTitle p{margin:0;color:var(--muted);font-size:12px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}.card{background:var(--panel);border:1px solid #26262c;border-radius:12px;overflow:hidden;cursor:pointer;transition:.16s;min-width:0}.card:hover{transform:translateY(-2px);border-color:#4b4b55}.thumb{aspect-ratio:16/9;background:linear-gradient(145deg,#24113f,#111);position:relative;overflow:hidden}.thumb img{width:100%;height:100%;object-fit:cover}.thumbFallback{position:absolute;inset:0;display:grid;place-items:center;font-size:32px;font-weight:900;color:#bf94ff}.cardBadge{position:absolute;left:9px;top:9px}.cardBody{padding:12px}.cardName{font-weight:800}.cardTitle{font-size:13px;color:#d1d1d7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:5px}.cardMeta{font-size:11px;color:var(--muted);margin-top:6px}.loading{animation:pulse 1.2s infinite alternate}@keyframes pulse{from{opacity:.45}to{opacity:1}}.toast{position:fixed;right:18px;bottom:18px;background:#1f1f23;border:1px solid #3a3a42;border-radius:10px;padding:12px 14px;box-shadow:0 20px 60px #0008;display:none;max-width:330px}.toast.show{display:block}.toast strong{display:block;margin-bottom:4px}.muted{color:var(--muted)}
@media(max-width:1000px){.app{grid-template-columns:230px 1fr}.hero{grid-template-columns:1fr}.infoCard{min-height:230px}}@media(max-width:720px){body{overflow:auto}.app{display:block;height:auto}.sidebar{position:sticky;top:0;z-index:20;height:auto;border-right:0}.brand{height:58px}.sideSearch,.sideBottom{display:none}.channelList{display:flex;overflow:auto;padding:8px}.channelRow{min-width:180px}.main{min-height:100vh}.topbar{display:none}.content{padding:12px}.hero{display:block}.infoCard{margin-top:12px}.grid{grid-template-columns:1fr 1fr}.playerShell{border-radius:10px}}@media(max-width:480px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand"><div class="brandMark">T</div><div><div class="brandTitle">Twitch IPTV</div><div class="brandSub">Cristian TV Hub</div></div></div>
    <div class="sideSearch"><div class="searchBox"><span>⌕</span><input id="search" placeholder="Cerca streamer"></div></div>
    <div id="channelList" class="channelList"><div class="muted loading" style="padding:16px">Caricamento canali…</div></div>
    <div class="sideBottom"><b id="serverDot">● Server</b> · {{base}}</div>
  </aside>

  <main class="main">
    <div class="topbar"><div class="topTitle">Twitch IPTV</div><div class="topActions"><button class="btn" onclick="copyText('{{base}}/playlist.m3u','Playlist copiata')">Copia M3U</button><button class="btn" onclick="copyText('{{base}}/epg.xml','EPG copiata')">Copia EPG</button><button class="btn primary" onclick="refreshAll(true)">Aggiorna</button></div></div>

    <div class="content">
      <section class="hero">
        <div class="playerShell" id="playerShell">
          <video id="video" controls playsinline></video>
          <div class="liveCorner" id="liveCorner"><span class="miniBadge" id="playerBadge">LIVE</span></div>
          <div class="playerEmpty" id="playerEmpty"><div><div class="emptyLogo">TV</div><div class="emptyTitle">Scegli un canale</div><div class="emptySub">Live Twitch quando è online, ultima VOD quando è offline.</div></div></div>
        </div>

        <div class="infoCard">
          <div class="channelHeader"><div class="bigAvatar" id="bigAvatar">TV</div><div><div class="channelName" id="channelName">Twitch IPTV</div><div class="statusText" id="statusText">Pronto</div></div></div>
          <div class="programTitle" id="programTitle">La tua TV Twitch privata</div>
          <div class="meta" id="programMeta">Seleziona uno streamer dalla barra laterale o dalla griglia.</div>
          <div class="infoSpacer"></div>
          <div class="quickGrid">
            <button class="quick" onclick="fullscreenPlayer()">Schermo intero<span>Modalità TV</span></button>
            <button class="quick" onclick="refreshCurrent()">Ricarica canale<span>Controlla LIVE/REPLAY</span></button>
          </div>
        </div>
      </section>

      <div class="sectionTitle"><div><h2>Tutti i canali</h2><p>Stato aggiornato automaticamente</p></div><p id="lastUpdate"></p></div>
      <section id="grid" class="grid"><div class="muted loading">Sto controllando Twitch…</div></section>
    </div>
  </main>
</div>
<div id="toast" class="toast"></div>

<script>
let channels=[]; let current=null; let hls=null; let poll=null;
const el=id=>document.getElementById(id);
function initials(name){return (name||'TV').replace(/[^a-z0-9]/gi,'').slice(0,2).toUpperCase()||'TV'}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}
function badgeClass(s){return s==='LIVE'?'live':s==='REPLAY'?'replay':'offline'}
function toast(title,msg=''){const t=el('toast');t.innerHTML=`<strong>${esc(title)}</strong><div class="muted">${esc(msg)}</div>`;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),3200)}
async function copyText(text,label){try{await navigator.clipboard.writeText(text);toast(label,text)}catch{prompt(label,text)}}
function avatarHtml(c,cls='avatar'){if(c.thumbnail)return `<div class="${cls}"><img src="${esc(c.thumbnail)}" loading="lazy"></div>`;return `<div class="${cls}">${initials(c.streamer)}</div>`}
function render(){
 const q=el('search').value.trim().toLowerCase(); const list=channels.filter(c=>c.streamer.toLowerCase().includes(q));
 el('channelList').innerHTML=list.map(c=>`<button class="channelRow ${current&&current.streamer===c.streamer?'active':''}" onclick="playChannel('${encodeURIComponent(c.streamer)}')">${avatarHtml(c)}<div><div class="rowName">${esc(c.streamer)}</div><div class="rowTitle">${esc(c.title||'')}</div></div><span class="badge ${badgeClass(c.status)}">${esc(c.status)}</span></button>`).join('')||'<div class="muted" style="padding:16px">Nessun risultato</div>';
 el('grid').innerHTML=list.map(c=>`<article class="card" onclick="playChannel('${encodeURIComponent(c.streamer)}')"><div class="thumb">${c.thumbnail?`<img src="${esc(c.thumbnail)}" loading="lazy">`:`<div class="thumbFallback">${initials(c.streamer)}</div>`}<span class="badge cardBadge ${badgeClass(c.status)}">${esc(c.status)}</span></div><div class="cardBody"><div class="cardName">${esc(c.streamer)}</div><div class="cardTitle">${esc(c.title||'')}</div><div class="cardMeta">${c.status==='REPLAY'&&c.date?'Live del '+esc(c.date):c.status==='LIVE'?'In diretta adesso':'Nessun contenuto disponibile'}</div></div></article>`).join('')||'<div class="muted">Nessun canale</div>';
}
async function refreshAll(manual=false){
 if(manual) toast('Aggiornamento','Controllo tutti i canali…');
 try{const r=await fetch('/api/channels',{cache:'no-store'}); channels=await r.json(); render(); el('lastUpdate').textContent='Aggiornato '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); if(current){const newer=channels.find(c=>c.streamer===current.streamer); if(newer&&newer.status!==current.status){toast(`${newer.streamer}: ${newer.status}`,newer.status==='LIVE'?'È tornato in diretta.':'Stato del canale cambiato.')}}}
 catch(e){toast('Errore','Impossibile aggiornare i canali.');}
}
function setHero(c){
 current=c; render(); el('channelName').textContent=c.streamer; el('statusText').textContent=c.status==='LIVE'?'In diretta su Twitch':c.status==='REPLAY'?(c.date?'Replay · Live del '+c.date:'Replay dell’ultima live'):'Offline'; el('programTitle').textContent=c.title||'Nessun titolo'; el('programMeta').textContent=c.status==='LIVE'?'Stai guardando la live corrente.':c.status==='REPLAY'?'Quando il canale è offline Twitch IPTV riproduce automaticamente l’ultima VOD disponibile.':'Nessuna VOD disponibile per questo canale.';
 const av=el('bigAvatar'); av.innerHTML=c.thumbnail?`<img src="${esc(c.thumbnail)}">`:initials(c.streamer);
 const b=el('playerBadge'); b.textContent=c.status; b.className='miniBadge '+(c.status==='LIVE'?'live':''); el('liveCorner').classList.toggle('show',c.status!=='OFFLINE');
}
async function playChannel(encoded){
 const name=decodeURIComponent(encoded); let c=channels.find(x=>x.streamer===name)||{streamer:name,status:'OFFLINE',title:'Caricamento…'}; setHero(c); el('playerEmpty').classList.remove('hidden'); el('playerEmpty').innerHTML='<div><div class="emptyLogo">…</div><div class="emptyTitle">Apro '+esc(name)+'</div><div class="emptySub">Controllo live e ultima VOD…</div></div>';
 try{const r=await fetch('/api/play/'+encodeURIComponent(name),{cache:'no-store'}); const data=await r.json(); if(!r.ok) throw new Error(data.error||'Stream non disponibile'); c={...c,...data}; setHero(c); startVideo(data.url); localStorage.setItem('lastChannel',name)}catch(e){stopVideo(); el('playerEmpty').classList.remove('hidden'); el('playerEmpty').innerHTML='<div><div class="emptyLogo">OFF</div><div class="emptyTitle">Canale non disponibile</div><div class="emptySub">'+esc(e.message)+'</div></div>'; toast(name,e.message)}
}
function startVideo(url){
 const video=el('video'); stopVideo(false); el('playerEmpty').classList.add('hidden');
 if(url.includes('/relay/')){video.src=url; video.play().catch(()=>{}); return}
 if(video.canPlayType('application/vnd.apple.mpegurl')){video.src=url; video.play().catch(()=>{}); return}
 if(window.Hls&&Hls.isSupported()){hls=new Hls({enableWorker:true,lowLatencyMode:true,maxBufferLength:25}); hls.loadSource(url); hls.attachMedia(video); hls.on(Hls.Events.MANIFEST_PARSED,()=>video.play().catch(()=>{})); hls.on(Hls.Events.ERROR,(event,data)=>{if(data.fatal){if(data.type===Hls.ErrorTypes.NETWORK_ERROR){hls.startLoad()}else if(data.type===Hls.ErrorTypes.MEDIA_ERROR){hls.recoverMediaError()}else{toast('Player','Errore HLS');}}}); return}
 video.src=url; video.play().catch(()=>{});
}
function stopVideo(clear=true){const v=el('video'); if(hls){hls.destroy();hls=null} v.pause();v.removeAttribute('src');v.load(); if(clear)el('playerEmpty').classList.remove('hidden')}
async function refreshCurrent(){if(!current)return; await refreshAll(false); playChannel(encodeURIComponent(current.streamer))}
function fullscreenPlayer(){const p=el('playerShell'); if(document.fullscreenElement)document.exitFullscreen(); else p.requestFullscreen?.()}
el('search').addEventListener('input',render);
(async()=>{await refreshAll(false); const last=localStorage.getItem('lastChannel'); if(last&&channels.some(c=>c.streamer===last)) playChannel(encodeURIComponent(last)); poll=setInterval(()=>refreshAll(false),60000)})();
</script>
</body>
</html>'''


if __name__ == "__main__":
    safe_console("")
    safe_console("========================================")
    safe_console("          TWITCH IPTV - CRISTIAN")
    safe_console("========================================")
    safe_console(f"Porta locale: {PORT}")
    safe_console("Su Railway usa il dominio pubblico generato.")
    safe_console("========================================")
    safe_console("")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
