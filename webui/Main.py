import streamlit as st
import os, json, subprocess, requests, re, asyncio
import time as time_module
import edge_tts

# ── Colab / Jupyter / Streamlit async compatibility ──────────────────────────
try:
    import nest_asyncio
    nest_asyncio.apply()
except (ImportError, ValueError):
    # ValueError = uvloop.Loop can't be patched — that's fine.
    # uvloop already handles concurrency natively, no patch needed.
    pass

def _run_async(coro):
    """Run async coroutine safely from sync code.
    Works with: standard asyncio, uvloop, Streamlit, Colab — all cases."""
    def _run_in_fresh_loop(c):
        """Always create a brand-new standard asyncio loop (avoids uvloop)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(c)
        finally:
            try:
                loop.close()
            except Exception:
                pass

    try:
        asyncio.get_running_loop()
        # A loop IS already running (uvloop / Streamlit / Jupyter).
        # Run in a new thread with a fresh standard asyncio loop.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run_in_fresh_loop, coro)
            return future.result()
    except RuntimeError:
        # No running loop — safe to create fresh one directly.
        return _run_in_fresh_loop(coro)
# ─────────────────────────────────────────────────────────────────────────────

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")

def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except:
        return {"openai_api_key":"","openrouter_api_key":"",
                "pexels_api_key":"","pixabay_api_key":"",
                "llm_provider":"OpenRouter",
                "model_name":"mistralai/mistral-7b-instruct:free",
                "base_url":"https://openrouter.ai/api/v1",
                "user_name":"Creator",
                "elevenlabs_api_key":""}

def save_config(data):
    try:
        with open(CONFIG_PATH,"w") as f:
            json.dump(data,f,indent=2)
        return True
    except: return False

EMOTION_PRESETS = {
    "😐 Neutral":      {"speed":50,"deep":50, "el_stability":0.50,"el_style":0.00},
    "😢 Sad / Poetic": {"speed":38,"deep":72, "el_stability":0.40,"el_style":0.40},
    "🔥 Intense":      {"speed":58,"deep":60, "el_stability":0.30,"el_style":0.70},
    "😌 Calm":         {"speed":42,"deep":45, "el_stability":0.70,"el_style":0.10},
    "⚡ Energetic":    {"speed":68,"deep":40, "el_stability":0.30,"el_style":0.55},
    "🎭 Dramatic":     {"speed":35,"deep":78, "el_stability":0.35,"el_style":0.60},
}

# ── ElevenLabs Integration ────────────────────────────────────────────────────
ELEVENLABS_VOICES = {
    "Adam — Deep & Serious":      "pNInz6obpgDQGcFmaJgB",
    "Antoni — Soft & Emotional":  "ErXwobaYiN019PkySvjV",
    "Josh — Dynamic & Clear":     "TxGEqnHWrfWFTfGW9XjX",
    "Rachel — Calm & Smooth":     "21m00Tcm4TlvDq8ikWAM",
    "Domi — Strong & Confident":  "AZnzlk1XvdvUeBnXmlld",
    "Elli — Female & Expressive": "MF3mGyEYCl7XYWbV9V6O",
    "Sam — Narrator Style":       "yoZ06aMxZJJ28mfd3POQ",
}

def generate_simple_vtt(text, duration, ts):
    """Time-distributed VTT for ElevenLabs (no word boundaries available)."""
    vtt_path = f"/tmp/subs_{ts}.vtt"
    words = clean_script(text).split()
    if not words: return None
    tpw = duration / len(words)
    with open(vtt_path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for i, w in enumerate(words):
            s = i * tpw
            e = max(min((i+1)*tpw - 0.05, duration), s + 0.10)
            f.write(f"{int(s//60):02d}:{s%60:06.3f} --> {int(e//60):02d}:{e%60:06.3f}\n{w}\n\n")
    return vtt_path

def generate_voice_elevenlabs(text, voice_id, stability, style, cfg):
    """Generate emotionally expressive voice via ElevenLabs API."""
    api_key = (cfg.get("elevenlabs_api_key","")
               or os.environ.get("ELEVENLABS_API_KEY",""))
    if not api_key:
        return None, None
    ts  = int(time_module.time())
    vp  = f"/tmp/voice_{ts}.mp3"
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "Accept":        "audio/mpeg",
        "Content-Type":  "application/json",
        "xi-api-key":    api_key,
    }
    data = {
        "text":     text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability":        round(stability / 100, 2),
            "similarity_boost": 0.75,
            "style":            round(style / 100, 2),
            "use_speaker_boost": True,
        },
    }
    try:
        r = requests.post(url, json=data, headers=headers, timeout=90)
        if r.status_code == 200:
            open(vp,"wb").write(r.content)
            dur = get_duration(vp)
            vtt = generate_simple_vtt(text, dur, ts)
            return vp, vtt
        else:
            print(f"[ElevenLabs] {r.status_code}: {r.text[:300]}")
            return None, None
    except Exception as e:
        print(f"[ElevenLabs] {e}")
        return None, None
# ─────────────────────────────────────────────────────────────────────────────

def clean_script(s):
    if not s or not isinstance(s, str): return ""
    s = re.sub(r'\[.*?\]','',s)
    s = re.sub(r'\*{1,2}(.*?)\*{1,2}',r'\1',s)
    s = re.sub(r'#+\s*','',s)
    s = re.sub(r'\n+',' ',s)
    return re.sub(r'\s+',' ',s).strip()

def get_rate_pitch(speed, deep):
    rate = int((speed - 50) * 1)
    rate_str = f"+{rate}%" if rate >= 0 else f"{rate}%"
    pitch = int((50 - deep) * 0.8)
    pitch_str = f"+{pitch}Hz" if pitch >= 0 else f"{pitch}Hz"
    return rate_str, pitch_str

async def _async_preview(voice, text, tmp, rate_str, pitch_str):
    communicate = edge_tts.Communicate(text, voice, rate=rate_str, pitch=pitch_str)
    await communicate.save(tmp)

def run_preview(voice, text, speed=50, deep=50):
    ts = int(time_module.time())
    tmp = f"/tmp/prev_{ts}.mp3"
    rate_str, pitch_str = get_rate_pitch(speed, deep)
    _run_async(_async_preview(voice, text, tmp, rate_str, pitch_str))
    with open(tmp, "rb") as f:
        return f.read()

async def _async_generate_voice(text, voice, vp, vtt, rate_str, pitch_str):
    """Generate TTS audio and a word-by-word VTT (one word per cue, exact timing)."""
    communicate = edge_tts.Communicate(text, voice, rate=rate_str, pitch=pitch_str)
    word_boundaries = []          # list of {word, start_sec, dur_sec}

    with open(vp, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                # Microsoft returns offset/duration in 100-nanosecond ticks
                word_boundaries.append({
                    "word":  chunk["text"],
                    "start": chunk["offset"]   / 10_000_000,
                    "dur":   chunk["duration"] / 10_000_000,
                })

    # Write VTT: one word per cue, no overlap, minimum 100 ms display
    if word_boundaries and vtt:
        with open(vtt, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n")
            for i, wb in enumerate(word_boundaries):
                s = wb["start"]
                e = s + wb["dur"]
                # End just before next word to prevent screen overlap
                if i + 1 < len(word_boundaries):
                    e = min(e, word_boundaries[i + 1]["start"] - 0.05)
                e = max(e, s + 0.10)     # at least 100 ms visible
                sm = f"{int(s // 60):02d}:{s % 60:06.3f}"
                em = f"{int(e // 60):02d}:{e % 60:06.3f}"
                f.write(f"{sm} --> {em}\n{wb['word']}\n\n")

def _generate_voice_elevenlabs(script, voice_id, cfg, stability=0.5, style=0.0):
    """ElevenLabs TTS with word-level timestamps for subtitle sync."""
    import base64
    api_key = cfg.get("elevenlabs_api_key","") if cfg else ""
    if not api_key: return None, None
    ts = int(time_module.time())
    vp  = f"/tmp/voice_{ts}.mp3"
    vtt = f"/tmp/subs_{ts}.vtt"
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps",
            headers={"xi-api-key":api_key,"Content-Type":"application/json"},
            json={"text":script,"model_id":"eleven_multilingual_v2",
                  "voice_settings":{"stability":stability,"similarity_boost":0.75,
                                    "style":style,"use_speaker_boost":True}},
            timeout=90)
        if r.status_code != 200:
            print(f"[ElevenLabs] {r.status_code}: {r.text[:200]}")
            return None, None
        data   = r.json()
        audio  = base64.b64decode(data.get("audio_base64",""))
        if not audio: return None, None
        with open(vp,"wb") as f: f.write(audio)
        # Build word-level VTT from character timestamps
        aln    = data.get("alignment",{})
        chars  = aln.get("characters",[])
        starts = aln.get("character_start_times_seconds",[])
        ends   = aln.get("character_end_times_seconds",[])
        if chars and starts:
            wbs, cur, ws = [], "", None
            for i, ch in enumerate(chars):
                if ch in (" ", "\n", "\t"):
                    if cur.strip() and ws is not None:
                        wbs.append({"word":cur.strip(),"start":ws,
                                    "dur":(ends[i-1] if i > 0 else ws)-ws})
                    cur, ws = "", None
                else:
                    if not cur: ws = starts[i]
                    cur += ch
            if cur.strip() and ws is not None:
                wbs.append({"word":cur.strip(),"start":ws,
                            "dur":(ends[-1] if ends else ws+0.2)-ws})
            with open(vtt,"w",encoding="utf-8") as f:
                f.write("WEBVTT\n\n")
                for i, wb in enumerate(wbs):
                    s=wb["start"]; e=s+max(wb["dur"],0.05)
                    if i+1<len(wbs): e=min(e, wbs[i+1]["start"]-0.05)
                    e=max(e, s+0.10)
                    f.write(f"{int(s//60):02d}:{s%60:06.3f} --> "
                            f"{int(e//60):02d}:{e%60:06.3f}\n{wb['word']}\n\n")
        return vp, (vtt if os.path.exists(vtt) else None)
    except Exception as ex:
        print(f"[ElevenLabs] Exception: {ex}")
        return None, None

def generate_voice(script, voice, speed=50, deep=50, cfg=None,
                   el_stability=0.5, el_style=0.0):
    clean = clean_script(script)
    if voice.startswith("EL:") and cfg:
        vp, vtt = _generate_voice_elevenlabs(
            clean, voice[3:], cfg, el_stability, el_style)
        if vp and os.path.exists(vp):
            return vp, vtt
        # Fallback to free edge-tts
        voice = "en-US-AriaNeural"
    ts = int(time_module.time())
    vp  = f"/tmp/voice_{ts}.mp3"
    vtt = f"/tmp/subs_{ts}.vtt"
    rate_str, pitch_str = get_rate_pitch(speed, deep)
    _run_async(_async_generate_voice(clean, voice, vp, vtt, rate_str, pitch_str))
    return vp, (vtt if os.path.exists(vtt) else None)

def vtt_to_sec(t):
    try:
        t = t.strip().replace(',','.')
        p = t.split(':')
        if len(p)==3: return float(p[0])*3600+float(p[1])*60+float(p[2])
        elif len(p)==2: return float(p[0])*60+float(p[1])
    except: return 0.0
    return 0.0

def sec_to_srt(t):
    h,m,s,ms=int(t//3600),int((t%3600)//60),int(t%60),int((t%1)*1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def parse_vtt(vtt_path):
    words = []
    try:
        with open(vtt_path,'r',encoding='utf-8') as f:
            content = f.read()
        for block in content.strip().split('\n\n'):
            lines = [l.strip() for l in block.split('\n') if l.strip()]
            for i, line in enumerate(lines):
                if '-->' in line:
                    parts = line.split('-->')
                    start = vtt_to_sec(parts[0])
                    end = vtt_to_sec(parts[1].split()[0])
                    texts = lines[i+1:]
                    text = re.sub(r'<[^>]+>','', ' '.join(texts)).strip()
                    if text: words.append((start, end, text))
                    break
    except: pass
    return words

def get_duration(path):
    r = subprocess.run(["ffprobe","-v","quiet","-show_entries",
                        "format=duration","-of","csv=p=0",path],
                       capture_output=True, text=True)
    try: return float(r.stdout.strip())
    except: return 30.0

def generate_bg_music(duration, style, volume):
    """Generate background music using simple sine waves — works on all ffmpeg versions."""
    if style == "No Music" or volume == 0: return None
    path = "/tmp/bg_music.aac"
    vol  = round((volume / 100.0) * 0.20, 4)
    dur  = int(duration) + 5

    # Base + harmonic frequency per style
    freq_pairs = {
        "Calm":      (220, 330),
        "Upbeat":    (330, 440),
        "Cinematic": (110, 165),
        "Random":    (220, 275),
    }
    f1, f2 = freq_pairs.get(style, freq_pairs["Calm"])

    # Two separate mono files, then mix — no weights parameter needed
    p1, p2 = "/tmp/_bg1.aac", "/tmp/_bg2.aac"
    for freq, fpath in [(f1, p1), (f2, p2)]:
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"sine=frequency={freq}:duration={dur}:sample_rate=44100",
            "-c:a", "aac", "-ar", "44100", fpath
        ], capture_output=True)

    if not os.path.exists(p1) or not os.path.exists(p2):
        return None

    cmd = [
        "ffmpeg", "-y",
        "-i", p1, "-i", p2,
        "-filter_complex",
        f"[0:a][1:a]amix=inputs=2:duration=longest:normalize=0,volume={vol},"
        f"afade=t=in:d=2,afade=t=out:st={max(dur-2,1)}:d=2[out]",
        "-map", "[out]",
        "-c:a", "aac", "-ar", "44100", "-b:a", "128k", path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if os.path.exists(path) and os.path.getsize(path) > 500:
        return path
    print(f"[BG Music] stderr: {result.stderr[-300:]}")
    return None

# ── Urdu/Arabic font + ASS subtitle builder ──────────────────────────────────
def ensure_urdu_font():
    """Get Urdu-capable font. Checks system first, then downloads."""
    dst = "/tmp/urdu_font.ttf"
    if os.path.exists(dst) and os.path.getsize(dst) > 50_000:
        return dst

    # 1. Try system-installed fonts (apt-get install fonts-noto installs these)
    try:
        r = subprocess.run(["fc-list", ":lang=ur", "--format=%{file}\n"],
                           capture_output=True, text=True, timeout=8)
        for line in r.stdout.strip().split("\n"):
            line = line.strip()
            if line and os.path.exists(line) and os.path.getsize(line) > 50_000:
                import shutil; shutil.copy(line, dst)
                return dst
    except Exception:
        pass

    # 2. Try Arabic fonts as fallback
    try:
        r = subprocess.run(["fc-list", ":lang=ar", "--format=%{file}\n"],
                           capture_output=True, text=True, timeout=8)
        for line in r.stdout.strip().split("\n"):
            line = line.strip()
            if line and os.path.exists(line) and os.path.getsize(line) > 50_000:
                import shutil; shutil.copy(line, dst)
                return dst
    except Exception:
        pass

    # 3. Download as last resort
    for url in [
        "https://fonts.gstatic.com/s/notonastalijurdu/v19/LhWNMUPbN-oZdNFcBy1-DJYsEoTq5pudQ4aa.ttf",
        "https://github.com/google/fonts/raw/main/ofl/notosansarabic/NotoSansArabic-Regular.ttf",
    ]:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200 and len(r.content) > 50_000:
                open(dst,"wb").write(r.content)
                return dst
        except Exception:
            continue
    return None

def get_urdu_font_family(font_path):
    """Get exact family name from font file using fc-query."""
    try:
        r = subprocess.run(["fc-query","--format=%{family}\n", font_path],
                           capture_output=True, text=True, timeout=5)
        fam = r.stdout.strip().split("\n")[0].strip()
        if fam: return fam
    except Exception:
        pass
    return "Noto Nastaliq Urdu"

def build_ass(words, font_size, color_hex, position, language, ass_path):
    """Build ASS subtitle file — proper font per language, RTL support."""
    try:
        hx = color_hex.lstrip("#")
        r2,g2,b2 = int(hx[:2],16), int(hx[2:4],16), int(hx[4:],16)
        pc = f"&H00{b2:02X}{g2:02X}{r2:02X}&"
    except:
        pc = "&H00FFFFFF&"

    is_rtl = language in ("Urdu","Arabic")
    al  = 6 if is_rtl else {"Bottom":2,"Center":5,"Top":8}.get(position, 2)

    # Font: get from system using fc-query for exact family name
    if is_rtl:
        font_file = ensure_urdu_font()
        fn = get_urdu_font_family(font_file) if font_file else "Noto Nastaliq Urdu"
        fonts_dir = os.path.dirname(font_file) if font_file else "/usr/share/fonts"
    else:
        font_file = None
        fn = "Noto Sans"
        fonts_dir = "/usr/share/fonts"

    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: Default,{fn},{font_size},{pc},&H00000000&,&H80000000&,"
        f"1,0,0,0,100,100,0,0,1,2,1,{al},10,10,50,1\n\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )

    def ts(s):
        return f"{int(s//3600)}:{int((s%3600)//60):02d}:{s%60:05.2f}"

    body = "".join(
        f"Dialogue: 0,{ts(s)},{ts(e)},Default,,0,0,0,,{w}\n"
        for s, e, w in words)

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + body)
    return ass_path, fonts_dir

VIBE_KEYWORDS = {
    "🌑 Dark":         "dark night shadows mystery noir",
    "✨ Aesthetic":    "aesthetic dreamy beautiful soft light",
    "🎬 Cinematic":    "cinematic dramatic landscape epic sky",
    "🌿 Nature":       "peaceful nature forest calm ocean",
    "💪 Motivational": "success achievement city sunrise powerful",
    "😢 Melancholic":  "rain lonely empty street dark moody",
    "💕 Romantic":     "sunset roses soft bokeh warm light",
    "🎭 Poetic":       "poetry book journal night lamp writing",
    "🏙️ Urban":        "city street urban modern neon",
    "🔥 Intense":      "fire dramatic intense powerful energy",
}

def fetch_pexels(keyword, count, cfg, media_type="video", vibe=""):
    key = cfg.get("pexels_api_key") or os.environ.get("PEXELS_API_KEY","")
    # Combine topic keyword + vibe keywords for better matching
    vibe_kw = VIBE_KEYWORDS.get(vibe,"")
    query = f"{keyword} {vibe_kw}".strip() if vibe_kw else keyword

    urls = []
    if media_type in ("video","both"):
        try:
            r = requests.get("https://api.pexels.com/videos/search",
                headers={"Authorization":key},
                params={"query":query,"per_page":count,
                        "orientation":"portrait","size":"medium"},
                timeout=15)
            for v in r.json().get("videos",[]):
                for f in v.get("video_files",[]):
                    if f.get("quality") in ["hd","sd"] and f.get("file_type")=="video/mp4":
                        urls.append(("video", f["link"])); break
        except Exception as e:
            print(f"[Pexels video] {e}")

    if media_type in ("photo","both"):
        try:
            r2 = requests.get("https://api.pexels.com/v1/search",
                headers={"Authori
