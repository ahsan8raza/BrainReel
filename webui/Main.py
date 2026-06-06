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
                headers={"Authorization":key},
                params={"query":query,"per_page":count,"orientation":"portrait"},
                timeout=15)
            for p in r2.json().get("photos",[]):
                src = p.get("src",{}).get("large2x") or p.get("src",{}).get("large")
                if src:
                    urls.append(("photo", src))
        except Exception as e:
            print(f"[Pexels photo] {e}")

    return urls[:count]

def download_clip_or_photo(item, idx, dur_sec=5):
    """Download video clip OR convert photo to video."""
    mtype, url = item
    out = f"/tmp/clip_{idx}.mp4"
    if mtype == "video":
        r = requests.get(url, timeout=60, stream=True)
        raw = f"/tmp/raw_{idx}.mp4"
        with open(raw,"wb") as f:
            for chunk in r.iter_content(8192): f.write(chunk)
        # re-encode to standard format
        subprocess.run(["ffmpeg","-y","-i",raw,
                        "-c:v","libx264","-preset","ultrafast",
                        "-an","-t","10",out], capture_output=True)
    else:
        # Photo → 5-second video with slow zoom (Ken Burns effect)
        r = requests.get(url, timeout=30)
        img_path = f"/tmp/img_{idx}.jpg"
        with open(img_path,"wb") as f: f.write(r.content)
        subprocess.run([
            "ffmpeg","-y","-loop","1","-i",img_path,
            "-vf",f"scale=1080:1920:force_original_aspect_ratio=increase,"
                  f"crop=1080:1920,zoompan=z='min(zoom+0.0008,1.3)':d={dur_sec*25}:s=1080x1920",
            "-t",str(dur_sec),"-c:v","libx264","-preset","ultrafast","-an",
            out], capture_output=True)
    return out

def download_clip(url, idx):
    path = f"/tmp/clip_{idx}.mp4"
    r = requests.get(url, timeout=60, stream=True)
    with open(path,"wb") as f:
        for chunk in r.iter_content(8192): f.write(chunk)
    return path

def compose_video(clips, voice_path, vtt_path, aspect,
                  font_size, color, position, script, bg_style, bg_vol, max_dur_secs=None, language="English"):
    out = "/tmp/final.mp4"
    W,H = (1080,1920) if "9:16" in aspect else (1920,1080)
    # ── B2 FIX: enforce user-selected duration cap ───────────────────────────
    voice_dur = get_duration(voice_path)
    if max_dur_secs and voice_dur > max_dur_secs:
        trimmed = "/tmp/voice_trimmed.mp3"
        subprocess.run(["ffmpeg","-y","-i",voice_path,
                        "-t",str(max_dur_secs),"-c:a","copy",trimmed],
                       capture_output=True)
        if os.path.exists(trimmed) and os.path.getsize(trimmed) > 500:
            voice_path = trimmed
    dur = get_duration(voice_path)
    if max_dur_secs: dur = min(dur, max_dur_secs)   # hard cap
    # ─────────────────────────────────────────────────────────────────────────
    cdur = dur / max(len(clips), 1)

    scaled = []
    for i, clip in enumerate(clips):
        o = f"/tmp/sc_{i}.mp4"
        cd = get_duration(clip)
        loop = ["-stream_loop", str(int(cdur/max(cd,0.1))+2)] if cd < cdur else []
        subprocess.run(["ffmpeg","-y"]+loop+["-i",clip,"-t",str(cdur),
            "-vf",f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}",
            "-c:v","libx264","-preset","ultrafast","-an",o], capture_output=True)
        if os.path.exists(o): scaled.append(o)

    clist = "/tmp/cl.txt"
    with open(clist,"w") as f:
        for c in scaled: f.write(f"file '{c}'\n")
    cout = "/tmp/co.mp4"
    subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",clist,
                    "-t",str(dur),"-c:v","libx264","-preset","ultrafast",cout],
                   capture_output=True)

    words = parse_vtt(vtt_path) if vtt_path and os.path.exists(vtt_path) else []
    srt = "/tmp/ws.srt"
    hex_c = color.lstrip('#')
    try:
        r2,g2,b2 = int(hex_c[:2],16),int(hex_c[2:4],16),int(hex_c[4:],16)
        ass_c = f"&H00{b2:02X}{g2:02X}{r2:02X}&"
    except: ass_c = "&H00FFFFFF&"
    align = {"Bottom":2,"Center":5,"Top":8}.get(position, 2)

    # (SRT writing removed — build_ass() handles all subtitle rendering)

    # Build ASS subtitle file — proper font per language, RTL handled inside
    ass_path = "/tmp/ws.ass"
    _display_words = words if words else []
    if not _display_words:
        # Fallback: chunk the script evenly
        _cw = clean_script(script).split()
        _cs = 4
        _chunks = [" ".join(_cw[j:j+_cs]) for j in range(0, len(_cw), _cs)]
        _tpc = dur / max(len(_chunks), 1)
        _display_words = [(i*_tpc-_tpc, i*_tpc, ch)
                          for i, ch in enumerate(_chunks, 1)]
    _ass_file, _fonts_dir = build_ass(
        _display_words, font_size, color, position, language, ass_path)
    sub_f = f"ass={_ass_file}:fontsdir={_fonts_dir}"

    bg = generate_bg_music(dur, bg_style, bg_vol)

    if bg:
        cmd = ["ffmpeg","-y","-i",cout,"-i",voice_path,"-i",bg,
               "-t",str(dur),
               "-filter_complex",
               "[1:a]volume=1.0[va];[2:a]volume=0.5[ba];[va][ba]amix=inputs=2:duration=first:normalize=0[aout]",
               "-map","0:v","-map","[aout]",
               "-vf",sub_f,
               "-c:v","libx264","-preset","ultrafast","-c:a","aac",out]
    else:
        cmd = ["ffmpeg","-y","-i",cout,"-i",voice_path,
               "-t",str(dur),"-map","0:v","-map","1:a",
               "-vf",sub_f,
               "-c:v","libx264","-preset","ultrafast","-c:a","aac",out]

    subprocess.run(cmd, capture_output=True)

    if not os.path.exists(out) or os.path.getsize(out) < 1000:
        if bg:
            subprocess.run(["ffmpeg","-y","-i",cout,"-i",voice_path,"-i",bg,
                "-t",str(dur),
                "-filter_complex","[1:a]volume=1.0[va];[2:a]volume=0.5[ba];[va][ba]amix=inputs=2:duration=first:normalize=0[aout]",
                "-map","0:v","-map","[aout]",
                "-c:v","libx264","-preset","ultrafast","-c:a","aac",out], capture_output=True)
        else:
            subprocess.run(["ffmpeg","-y","-i",cout,"-i",voice_path,
                "-t",str(dur),"-map","0:v","-map","1:a",
                "-c:v","libx264","-preset","ultrafast","-c:a","aac",out], capture_output=True)
    return out

def generate_script_api(topic, duration, language, cfg):
    # Word counts with buffer so voice > target, then ffmpeg trims to exact length
    wc = {"30 seconds":"90-100","1 minute":"170-190",
          "3 minutes":"450-500","5 minutes":"720-780"}.get(duration,"170-190")

    # Per-language strict instruction — LLM ko force karo
    lang_rules = {
        "English": "Write ONLY in English.",
        "Urdu":    "تمام مواد صرف اردو زبان میں لکھیں۔ WRITE ONLY IN URDU LANGUAGE (Urdu script). Do NOT use English words at all. Every single word must be Urdu.",
        "Hindi":   "केवल हिंदी भाषा में लिखें। WRITE ONLY IN HINDI (Devanagari script). Do NOT use English.",
        "Arabic":  "اكتب باللغة العربية فقط. WRITE ONLY IN ARABIC. Do NOT use English.",
        "Chinese": "只用中文写作。WRITE ONLY IN CHINESE. Do NOT use English.",
    }
    lang_rule = lang_rules.get(language, lang_rules["English"])

    prompt = f"""You are a script writer. Write a faceless YouTube video script about: {topic}

LANGUAGE RULE (MOST IMPORTANT): {lang_rule}

Word count: EXACTLY {wc} words.
Format rules:
- Pure spoken narration only
- NO [brackets], NO markdown, NO headers, NO stage directions
- NO translation or English words (if non-English language selected)
- Start immediately with a strong hook sentence
- Smooth, natural speaking flow"""
    key = cfg.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY","")
    model = cfg.get("model_name") or "mistralai/mistral-7b-instruct:free"
    base = cfg.get("base_url") or "https://openrouter.ai/api/v1"
    try:
        resp = requests.post(f"{base}/chat/completions",
            headers={"Authorization":f"Bearer {key}","Content-Type":"application/json",
                     "HTTP-Referer":"https://huggingface.co","X-Title":"BrainReel"},
            json={"model":model,"messages":[{"role":"user","content":prompt}],"max_tokens":1500},
            timeout=60)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if choices:
            content_text = (choices[0].get("message") or {}).get("content","")
            if content_text and content_text.strip():
                return clean_script(content_text)
        return f"This is an informative video about {topic}. We will explore everything about this fascinating topic step by step."
    except Exception as e:
        return f"This is an informative video about {topic}. We will explore everything about this fascinating topic step by step." 

ETA_MAP = {"30 seconds":"~2 min","1 minute":"~3 min",
           "3 minutes":"~7 min","5 minutes":"~15 min"}

# Compose-step estimated seconds (longest step)
COMPOSE_ETA = {"30 seconds":90,"1 minute":150,
               "3 minutes":360,"5 minutes":600}

def _fmt_time(secs):
    """Format seconds → '1m 23s' or '45s'."""
    secs = max(0, int(secs))
    return f"{secs//60}m {secs%60}s" if secs >= 60 else f"{secs}s"

# Hard duration cap in seconds — enforced via ffmpeg trim
DURATION_SECS = {"30 seconds":30,"1 minute":60,
                 "3 minutes":180,"5 minutes":300}

# ElevenLabs voice IDs — needs ELEVENLABS_KEY in Settings
_EL = {
    "👑 ElevenLabs — Rachel (Female, Natural)": "EL:21m00Tcm4TlvDq8ikWAM",
    "👑 ElevenLabs — Adam (Male, Deep)":        "EL:pNInz6obpgDQGcFmaJgB",
    "👑 ElevenLabs — Antoni (Male, Warm)":      "EL:ErXwobaYiN019PkySvjV",
    "👑 ElevenLabs — Bella (Female, Soft)":     "EL:EXAVITQu4vr4xnSDxMaL",
    "👑 ElevenLabs — Josh (Male, Young)":       "EL:TxGEqnHWrfWFTfGW9XjX",
    "👑 ElevenLabs — Elli (Female, Emotional)": "EL:MF3mGyEYCl7XYWbV9V6O",
}
VOICE_MAP = {**_EL, **{
    "en-US-AriaNeural — English Female 🇺🇸":"en-US-AriaNeural",
    "en-US-GuyNeural — English Male 🇺🇸":"en-US-GuyNeural",
    "ur-PK-UzmaNeural — Urdu Female 🇵🇰":"ur-PK-UzmaNeural",
    "ur-PK-AsadNeural — Urdu Male 🇵🇰":"ur-PK-AsadNeural",
    "hi-IN-SwaraNeural — Hindi Female 🇮🇳":"hi-IN-SwaraNeural",
    "hi-IN-MadhurNeural — Hindi Male 🇮🇳":"hi-IN-MadhurNeural",
    "ar-SA-ZariyahNeural — Arabic Female 🇸🇦":"ar-SA-ZariyahNeural",
    "zh-CN-XiaoxiaoNeural — Chinese Female 🇨🇳":"zh-CN-XiaoxiaoNeural",
}}
PREVIEW_TEXTS = {
    "en-US-AriaNeural":"Hello! I am Aria. I will be the voice of your video.",
    "en-US-GuyNeural":"Hello! I am Guy. I will be the voice of your video.",
    "ur-PK-UzmaNeural":"السلام علیکم! میں آپ کی ویڈیو کی آواز ہوں۔",
    "ur-PK-AsadNeural":"السلام علیکم! میں آپ کی ویڈیو کی آواز ہوں۔",
    "hi-IN-SwaraNeural":"नमस्ते! मैं आपके वीडियो की आवाज़ हूँ।",
    "hi-IN-MadhurNeural":"नमस्ते! मैं आपके वीडियो की आवाज़ हूँ।",
    "ar-SA-ZariyahNeural":"مرحبا! أنا صوت الفيديو الخاص بك.",
    "zh-CN-XiaoxiaoNeural":"你好！我是你视频的声音。",
}
MODEL_LISTS = {
    "OpenAI":["gpt-4o","gpt-4o-mini","gpt-4-turbo","gpt-3.5-turbo","Custom (type below)"],
    "OpenRouter":["deepseek/deepseek-chat","deepseek/deepseek-r1",
                  "google/gemini-flash-1.5","google/gemini-pro-1.5",
                  "meta-llama/llama-3.1-8b-instruct:free",
                  "meta-llama/llama-3.3-70b-instruct",
                  "mistralai/mixtral-8x7b-instruct",
                  "mistralai/mistral-7b-instruct:free",
                  "anthropic/claude-3-haiku","anthropic/claude-3.5-sonnet",
                  "qwen/qwen-2.5-72b-instruct",
                  "microsoft/phi-3-medium-128k-instruct:free",
                  "Custom (type below)"],
    "DeepSeek":["deepseek-chat","deepseek-reasoner","Custom (type below)"],
    "Moonshot":["moonshot-v1-8k","moonshot-v1-32k","moonshot-v1-128k","Custom (type below)"],
    "Google Gemini":["gemini-1.5-flash","gemini-1.5-pro","gemini-2.0-flash","Custom (type below)"],
    "Ollama":["llama3","llama3.1","mistral","qwen2.5","deepseek-r1","Custom (type below)"],
}

st.set_page_config(page_title="BrainReel",page_icon="🎬",
                   layout="wide",initial_sidebar_state="collapsed")

cfg = load_config()

# ══════════════════════════════════════
# WELCOME SCREEN
# ══════════════════════════════════════
if "welcome_done" not in st.session_state:
    st.session_state.welcome_done = False

if not st.session_state.welcome_done:
    user_name = cfg.get("user_name", "Creator")

    # ── Welcome page CSS ─────────────────────────────────────────────────────
    st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@300;400;700;900&family=Orbitron:wght@700;900&display=swap');
.stApp{background:#0B0F1A!important}
.main .block-container{padding:8px 16px!important;max-width:100%!important}
#MainMenu,footer,header{visibility:hidden!important}
/* ── X close button ── */
div[data-testid="stButton"].welcome-x > button{
    background:rgba(255,255,255,0.06)!important;
    border:1px solid rgba(255,255,255,0.12)!important;
    color:#888!important;border-radius:50%!important;
    width:38px!important;height:38px!important;
    min-width:38px!important;padding:0!important;
    font-size:18px!important;font-weight:400!important;
    letter-spacing:0!important;text-transform:none!important;
    line-height:1!important;transition:all 0.2s!important;
    box-shadow:none!important;}
div[data-testid="stButton"].welcome-x > button:hover{
    background:rgba(255,60,60,0.15)!important;
    border-color:rgba(255,80,80,0.4)!important;
    color:#ff6060!important;}
/* ── Enter button ── */
div[data-testid="stButton"].welcome-enter > button{
    background:linear-gradient(135deg,#1a3a6b,#2F80FF)!important;
    color:white!important;border:none!important;
    border-radius:30px!important;padding:15px 40px!important;
    font-size:13px!important;font-weight:700!important;
    letter-spacing:4px!important;text-transform:uppercase!important;
    box-shadow:0 0 30px #2F80FF50,0 4px 20px #2F80FF30!important;
    transition:all 0.3s!important;width:100%!important;}
div[data-testid="stButton"].welcome-enter > button:hover{
    box-shadow:0 0 45px #2F80FF80,0 6px 30px #2F80FF50!important;
    transform:translateY(-2px)!important;}
/* pulse animation on BRAINREEL */
@keyframes brPulse{
    0%,100%{text-shadow:0 0 30px #2F80FF90,0 0 60px #2F80FF40;}
    50%{text-shadow:0 0 50px #2F80FFcc,0 0 90px #2F80FF70,0 0 120px #2F80FF30;}}
.br-title{animation:brPulse 3s ease-in-out infinite;}
</style>""", unsafe_allow_html=True)

    # ── Top bar: X close button ───────────────────────────────────────────────
    _, _, x_col = st.columns([12, 1, 1])
    with x_col:
        st.markdown('<div class="welcome-x">', unsafe_allow_html=True)
        if st.button("✕", key="welcome_x"):
            st.session_state.welcome_done = True
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    # ── Welcome HTML ──────────────────────────────────────────────────────────
    st.markdown(f"""
<div style='min-height:75vh;background:#0B0F1A;display:flex;flex-direction:column;
    align-items:center;justify-content:center;text-align:center;
    padding:20px 20px 10px;font-family:Montserrat,sans-serif;'>

  <div style='letter-spacing:10px;font-size:14px;font-weight:300;color:#7ab4ff;
      margin-bottom:4px;text-shadow:0 0 18px #2F80FF,0 0 40px #2F80FF50;'>
    WELCOME</div>

  <div style='letter-spacing:6px;font-size:11px;font-weight:300;color:#3a4060;
      margin-bottom:10px;'>TO</div>

  <div class='br-title' style='letter-spacing:7px;font-size:50px;font-weight:900;
      color:#2F80FF;font-family:Orbitron,sans-serif;margin-bottom:6px;'>
    BRAINREEL</div>

  <div style='width:120px;height:1px;
      background:linear-gradient(90deg,transparent,#2F80FF,transparent);
      margin:16px auto 18px;'></div>

  <div style='font-size:12px;color:#8899bb;letter-spacing:5px;
      text-transform:uppercase;margin-bottom:24px;'>
    Make Your Life Fast With AI</div>

  <div style='font-size:19px;color:#ccc;margin-bottom:8px;'>
    Welcome back,&nbsp;
    <span style='color:#2F80FF;font-weight:700;
        text-shadow:0 0 12px #2F80FF60;'>{user_name}</span> 👋</div>

  <div style='font-size:11px;color:#556;letter-spacing:1px;margin-bottom:6px;'>
    🎬 AI-Powered Faceless Video Generator</div>

  <div style='font-size:10px;color:#2F80FF40;letter-spacing:2px;margin-bottom:36px;'>
    OpenRouter &nbsp;•&nbsp; Pexels &nbsp;•&nbsp; Edge-TTS &nbsp;•&nbsp; FFmpeg</div>

</div>""", unsafe_allow_html=True)

    # ── Enter button ──────────────────────────────────────────────────────────
    _, btn_col, _ = st.columns([2, 3, 2])
    with btn_col:
        st.markdown('<div class="welcome-enter">', unsafe_allow_html=True)
        if st.button("✨  ENTER  BRAINREEL  ✨",
                     key="welcome_enter", use_container_width=True):
            st.session_state.welcome_done = True
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    # ── Watermark ─────────────────────────────────────────────────────────────
    st.markdown("""
<div style='text-align:center;font-size:11px;color:#2F80FF;
    letter-spacing:4px;text-transform:uppercase;padding:18px 0 8px;
    font-weight:800;text-shadow:0 0 12px #2F80FF50;opacity:0.85;'>
  Revamped &amp; Engineered &mdash; Ahsan Raza
</div>""", unsafe_allow_html=True)

    st.stop()

# ══════════════════════════════════════
# MAIN APP CSS
# ══════════════════════════════════════
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
html,body,[class*="css"]{font-family:'Inter',sans-serif;background-color:#0e0e0e;color:#f0f0f0;}
#MainMenu{visibility:hidden;}footer{visibility:hidden;}
header{visibility:visible!important;}
[data-testid="stAppDeployButton"]{display:none!important;}
button[data-testid="collapsedControl"]{
    visibility:visible!important;display:flex!important;opacity:1!important;
    background:linear-gradient(135deg,#7c3aed,#2563eb)!important;
    border-radius:8px!important;border:none!important;
    width:40px!important;height:40px!important;
    box-shadow:0 4px 15px rgba(124,58,237,0.5)!important;}
button[data-testid="collapsedControl"] svg{fill:white!important;}
button[data-testid="expandedControl"]{
    visibility:visible!important;display:flex!important;opacity:1!important;
    background:rgba(255,255,255,0.1)!important;
    border:1px solid rgba(255,255,255,0.2)!important;
    border-radius:8px!important;width:36px!important;height:36px!important;}
button[data-testid="expandedControl"] svg{fill:#f0f0f0!important;}
section[data-testid="stSidebar"]{
    background:linear-gradient(180deg,#1a1a2e 0%,#16213e 100%);
    border-right:1px solid #2a2a4a;}
.brand-logo{text-align:center;padding:20px 0 10px 0;}
.brand-title{font-size:28px;font-weight:700;
    background:linear-gradient(90deg,#a78bfa,#60a5fa);
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;margin:0;}
.brand-sub{font-size:12px;color:#888;margin-top:4px;}
.card-title{font-size:14px;font-weight:600;color:#a78bfa;
    margin-bottom:12px;text-transform:uppercase;letter-spacing:1px;}
.stButton>button{
    background:linear-gradient(90deg,#7c3aed,#2563eb);
    color:white;border:none;border-radius:8px;
    font-weight:600;padding:10px 24px;width:100%;transition:opacity 0.2s;}
.stButton>button:hover{opacity:0.85;color:white;}
.stTextInput>div>div>input,
.stTextArea>div>div>textarea,
.stSelectbox>div>div{
    background:#0e0e1a!important;border:1px solid #2a2a4a!important;
    border-radius:8px!important;color:#f0f0f0!important;}
.stTabs [data-baseweb="tab-list"]{
    background:#1a1a2e;border-radius:10px;padding:4px;gap:4px;}
.stTabs [data-baseweb="tab"]{
    background:transparent;border-radius:8px;
    color:#888;font-weight:600;padding:14px 22px;font-size:15px;}
.stTabs [aria-selected="true"]{
    background:linear-gradient(90deg,#7c3aed,#2563eb)!important;color:white!important;}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;
    font-size:11px;font-weight:600;}
.badge-ready{background:#064e3b;color:#34d399;}
.badge-warn{background:#451a03;color:#fb923c;}
.section-header{font-size:11px;text-transform:uppercase;
    letter-spacing:2px;color:#555;margin:20px 0 8px 0;}
.key-status{font-size:12px;padding:4px 10px;border-radius:6px;
    margin-top:4px;display:inline-block;}
.key-set{background:#064e3b;color:#34d399;}
.key-empty{background:#1a1a2e;color:#555;}
.watermark{
    font-size:11px;color:#2F80FF;letter-spacing:3px;
    text-transform:uppercase;text-align:center;font-weight:800;
    padding:14px 6px 6px;border-top:1px solid #1a2840;
    text-shadow:0 0 10px #2F80FF50;
    opacity:0.9;}
/* ── Sidebar Navigation — bigger, bolder, active state ── */
div[data-testid="stSidebar"] div[data-testid="stRadio"]{gap:6px!important;}
div[data-testid="stSidebar"] div[data-testid="stRadio"] label{
    font-size:19px!important;font-weight:700!important;
    padding:15px 18px!important;margin:3px 0!important;
    border-radius:12px!important;display:flex!important;
    align-items:center!important;
    color:#b0b8d0!important;letter-spacing:0.4px!important;
    transition:all 0.2s ease!important;cursor:pointer!important;
    border-left:4px solid transparent!important;
    background:rgba(255,255,255,0.03)!important;
    min-height:52px!important;}
div[data-testid="stSidebar"] div[data-testid="stRadio"] label:hover{
    background:rgba(47,128,255,0.12)!important;
    color:#ffffff!important;
    border-left:4px solid #2F80FF!important;}
div[data-testid="stSidebar"] div[data-testid="stRadio"] label p{
    font-size:19px!important;font-weight:700!important;margin:0!important;}
/* Hide the small radio circle dot — keep it functional */
div[data-testid="stSidebar"] div[data-testid="stRadio"] span[data-testid="stRadioButton"]{
    display:none!important;}
div[data-testid="stSidebar"] div[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked),
div[data-testid="stSidebar"] div[data-testid="stRadio"] label:has(input[type="radio"]:checked){
    background:rgba(47,128,255,0.18)!important;
    color:#2F80FF!important;
    border-left:4px solid #2F80FF!important;
    font-weight:800!important;}
div[data-testid="stSidebar"] div[data-testid="stRadio"] label:has(input:checked) p{
    color:#2F80FF!important;}
</style>
""",unsafe_allow_html=True)

# ══════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════
with st.sidebar:
    st.markdown("""
    <div class="brand-logo">
        <div class="brand-title">🎬 BrainReel</div>
        <div class="brand-sub">AI Faceless Video Generator</div>
    </div>""",unsafe_allow_html=True)
    st.markdown("---")
    st.markdown('<div class="section-header">Navigation</div>',unsafe_allow_html=True)
    page = st.radio(label="",
                    options=["🎬  Generate Video","⚙️  Settings"],
                    label_visibility="collapsed")
    st.markdown("---")
    st.markdown('<div class="section-header">Status</div>',unsafe_allow_html=True)
    api_set = bool(cfg.get("openai_api_key") or cfg.get("openrouter_api_key")
                   or os.environ.get("OPENROUTER_API_KEY"))
    st.markdown(
        f'<span class="badge {"badge-ready" if api_set else "badge-warn"}">'
        f'{"✓ API Ready" if api_set else "⚠ API Not Set"}</span>',
        unsafe_allow_html=True)
    st.markdown("<br><br>",unsafe_allow_html=True)
    st.caption("v3.0 · BrainReel Edition")
    st.markdown(
        '<div class="watermark">Revamped &amp; Engineered — Ahsan Raza</div>',
        unsafe_allow_html=True)

# ══════════════════════════════════════
# GENERATE VIDEO PAGE
# ══════════════════════════════════════
if page == "🎬  Generate Video":
    st.markdown("## 🎬 Generate Video")
    st.caption("Fill in the details and let AI do the magic!")
    st.markdown(
        "<div style='font-size:10px;color:#2F80FF;letter-spacing:3px;"
        "text-transform:uppercase;font-weight:800;text-align:right;"
        "text-shadow:0 0 8px #2F80FF40;margin-bottom:4px;'>"
        "Revamped &amp; Engineered &mdash; Ahsan Raza</div>",
        unsafe_allow_html=True)

    tab1, tab2, tab3 = st.tabs(
        ["📝  Script", "🎨  Style", "🔊  Voice & Music"])

    with tab1:
        st.markdown('<div class="card-title">📝 Video Script</div>',
                    unsafe_allow_html=True)
        video_subject = st.text_input("Video Topic / Subject",
            placeholder="e.g. 5 mind-blowing facts about space")
        script_mode = st.radio("Script Mode",
            ["🤖 AI Auto Generate","✍️ Write Manually"], horizontal=True)
        if script_mode == "✍️ Write Manually":
            manual_script = st.text_area("Your Script",
                placeholder="Sirf narration text — koi [brackets] nahi...",
                height=180)
        else:
            manual_script = ""
        col1, col2 = st.columns(2)
        with col1:
            video_language = st.selectbox("Language",
                ["English","Urdu","Hindi","Arabic","Chinese"])
        with col2:
            video_length = st.selectbox("Video Length",
                ["30 seconds","1 minute","3 minutes","5 minutes"])

        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        video_vibe = st.selectbox(
            "🎨 Video Vibe",
            list(VIBE_KEYWORDS.keys()),
            help="Vibe ke hisaab se Pexels se matching videos/photos fetch honge")
        st.caption(f"Pexels search: **{VIBE_KEYWORDS.get(video_vibe,'')}**")

    with tab2:
        st.markdown('<div class="card-title">🎨 Visual Style</div>',
                    unsafe_allow_html=True)
        col1, col2 = st.columns(2)
        with col1:
            video_aspect = st.selectbox("Aspect Ratio",
                ["9:16 (Vertical / TikTok)","16:9 (Horizontal / YouTube)"])
        with col2:
            st.selectbox("Video Source",["Pexels (Free)"])
        st.markdown("**📸 Media Type**")
        media_type_opt = st.radio(
            "Media Type", ["🎬 Video", "📷 Photo", "🎞️ Mixed"],
            horizontal=True, label_visibility="collapsed",
            help="Video=Pexels videos | Photo=Pexels photos (Ken Burns zoom) | Mixed=dono")
        _media_map = {"🎬 Video":"video","📷 Photo":"photo","🎞️ Mixed":"both"}
        pexels_media = _media_map.get(media_type_opt, "video")
        st.markdown("**Subtitle Settings**")
        col3, col4 = st.columns(2)
        with col3:
            font_size = st.slider("Font Size", 20, 80, 42)
        with col4:
            subtitle_position = st.selectbox("Position",
                ["Bottom","Center","Top"])
        subtitle_color = st.color_picker("Subtitle Color","#FFFFFF")

    with tab3:
        st.markdown('<div class="card-title">🔊 Voice & Music</div>',
                    unsafe_allow_html=True)

        # ── TTS Engine selection ──────────────────────────────────────────────────
        tts_engine = st.radio(
            "🎙️ TTS Engine",
            ["Edge-TTS (Free)", "ElevenLabs 🎭 (Emotional)"],
            horizontal=True,
            help="ElevenLabs = real human-like emotions | Edge-TTS = free, fast")

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

        if tts_engine == "ElevenLabs 🎭 (Emotional)":
            el_key_set = bool(cfg.get("elevenlabs_api_key","") or
                              os.environ.get("ELEVENLABS_API_KEY",""))
            if not el_key_set:
                st.error("❌ ElevenLabs API key missing — Settings mein daalo! (elevenlabs.io se free key lo)")
            el_voice_name = st.selectbox("🎭 ElevenLabs Voice", list(ELEVENLABS_VOICES.keys()))
            el_voice_id   = ELEVENLABS_VOICES[el_voice_name]
            c1, c2 = st.columns(2)
            with c1:
                el_stability = st.slider("Stability", 0, 100, 30, format="%d%%",
                    help="Low=expressive/emotional | High=consistent/stable")
            with c2:
                el_style = st.slider("Style / Emotion", 0, 100, 65, format="%d%%",
                    help="High=more emotional expression")
            st.caption("💡 Urdu poetry ke liye: Stability **25%** + Style **70%** = max emotion")
            if st.button("🔊 Preview ElevenLabs Voice"):
                if el_key_set:
                    with st.spinner("🎭 ElevenLabs preview..."):
                        vp, _ = generate_voice_elevenlabs(
                            "Hello! This is a preview. I will narrate your story with emotion.",
                            el_voice_id, el_stability, el_style, cfg)
                        if vp:
                            with open(vp,"rb") as f: st.audio(f.read(), format="audio/mp3")
                            st.success("✅ Preview ready!")
                        else:
                            st.error("❌ ElevenLabs error — key check karo")
            # Set dummy edge-tts vars (not used when ElevenLabs selected)
            voice_display = list(VOICE_MAP.keys())[0]
            selected_voice = VOICE_MAP[voice_display]
            emotion_preset = "😐 Neutral"; _ep = EMOTION_PRESETS[emotion_preset]
            voice_speed = _ep["speed"]; voice_deep = _ep["deep"]
        else:
            el_voice_id = None; el_stability = 50; el_style = 50

        if tts_engine == "Edge-TTS (Free)":
            voice_display = st.selectbox("TTS Voice", list(VOICE_MAP.keys()))
            selected_voice = VOICE_MAP[voice_display]

            st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

            # ── Emotion Style ──────────────────────────────────────────────────
        st.markdown("""<div style='font-size:13px;color:#aaa;margin-bottom:2px'>
            🎭 <b>Voice Emotion Style</b></div>""", unsafe_allow_html=True)
        emotion_preset = st.selectbox(
            "Emotion Style", list(EMOTION_PRESETS.keys()),
            label_visibility="collapsed",
            help="Emotion ke hisaab se Speed & Deep auto-set ho jaenge")
        _ep = EMOTION_PRESETS[emotion_preset]
        # Caption showing what emotion does
        _emo_desc = {
            "😐 Neutral":"Normal reading speed",
            "😢 Sad / Poetic":"Slow, deep — perfect for poetry & emotional content",
            "🔥 Intense":"Fast, punchy — good for facts & drama",
            "😌 Calm":"Soft, gentle — meditation & nature content",
            "⚡ Energetic":"Upbeat, fast — motivation & news",
            "🎭 Dramatic":"Very slow & deep — cinematic narration",
        }
        st.caption(f"*{_emo_desc.get(emotion_preset,'')}*")

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

        # ── Voice Speed — default from emotion preset ────────────────────────────
        st.markdown("""<div style='font-size:13px;color:#aaa;margin-bottom:2px'>
            🏃 <b>Voice Speed</b></div>""", unsafe_allow_html=True)
        voice_speed = st.slider("Voice Speed", 1, 100, _ep["speed"],
            format="%d%%", label_visibility="collapsed",
            key=f"vs_{emotion_preset}",
            help="1%=Slowest | 50%=Normal | 100%=Fastest")
        spd_label = "🐢 Slow" if voice_speed < 35 else ("⚡ Fast" if voice_speed > 65 else "✅ Normal")
        st.caption(f"Speed: **{voice_speed}%** — {spd_label}")

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

        # ── Voice Deepness — default from emotion preset ─────────────────────────
        st.markdown("""<div style='font-size:13px;color:#aaa;margin-bottom:2px'>
            🎵 <b>Voice Deepness</b></div>""", unsafe_allow_html=True)
        voice_deep = st.slider("Voice Deepness", 1, 100, _ep["deep"],
            format="%d%%", label_visibility="collapsed",
            key=f"vd_{emotion_preset}",
            help="1%=Highest Pitch | 50%=Normal | 100%=Deepest")
        dp_label = "🔈 High pitch" if voice_deep < 35 else ("🔉 Deep" if voice_deep > 65 else "✅ Normal")
        st.caption(f"Deepness: **{voice_deep}%** — {dp_label}")

        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

        # ── Preview Button ───────────────────────────────────────────────────────
        if st.button("🔊 Preview Voice", use_container_width=True):
            with st.spinner("🎙️ Preview ban raha hai..."):
                try:
                    prev_text = PREVIEW_TEXTS.get(
                        selected_voice, "Hello! This is a preview.")
                    ab = run_preview(selected_voice, prev_text,
                                     voice_speed, voice_deep)
                    st.audio(ab, format="audio/mp3")
                    st.success("✅ Preview ready! Speed: {}% | Deep: {}%".format(
                        voice_speed, voice_deep))
                except Exception as e:
                    st.error(f"❌ {e}")

        st.markdown("---")

        col_m1, col_m2 = st.columns(2)
        with col_m1:
            bg_music = st.selectbox("🎵 Background Music",
                ["No Music","Calm","Upbeat","Cinematic","Random"])
        with col_m2:
            bg_volume = st.slider("🔉 BG Volume", 0, 100, 30,
                disabled=(bg_music == "No Music"))

        # ── Generate Section (bottom of Voice & Music tab) ──────────────────────
        st.markdown("---")

        # Quick settings summary
        _vname = voice_display.split("—")[0].strip()
        st.markdown(
            f"<div style='background:#1a1f2e;border:1px solid #2a3050;"
            f"border-radius:8px;padding:10px 14px;margin-bottom:10px;"
            f"font-size:13px;color:#bbb;line-height:1.8'>"
            f"<b style=\'color:#fff\'>📋 Video Summary</b><br>"
            f"⏱️ <b>{video_length}</b> &nbsp;|&nbsp; "
            f"🌐 <b>{video_language}</b> &nbsp;|&nbsp; "
            f"🎙️ <b>{_vname}</b><br>"
            f"🎭 <b>{emotion_preset}</b> &nbsp;|&nbsp; "
            f"🎨 <b>{video_vibe}</b> &nbsp;|&nbsp; "
            f"📸 <b>{media_type_opt}</b><br>"
            f"🏃 Speed: <b>{voice_speed}%</b> &nbsp;|&nbsp; "
            f"🎵 Deep: <b>{voice_deep}%</b> &nbsp;|&nbsp; "
            f"🎵 BG: <b>{bg_music}</b>"
            f"</div>",
            unsafe_allow_html=True)

        _eta = ETA_MAP.get(video_length, "~3 min")
        st.caption(f"⏱️ Estimated generation time: **{_eta}**")

        if st.session_state.get("gen_step", 0) > 0:
            _cr1, _cr2 = st.columns([4, 1])
            with _cr2:
                if st.button("🔄 Reset", use_container_width=True):
                    for k in ["gen_step","gen_script","gen_voice",
                              "gen_vtt","gen_clips","gen_output",
                              "gen_start","gen_step_times"]:
                        st.session_state.pop(k, None)
                    st.rerun()

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
        gen_btn = st.button(
            "🚀 Generate Video",
            use_container_width=True,
            type="primary")

        if gen_btn:
            if not video_subject and not manual_script:
                st.error("⚠️ Topic ya script enter karo pehle!")
            else:
                pexels_key = (cfg.get("pexels_api_key") or
                              os.environ.get("PEXELS_API_KEY",""))
                or_key = (cfg.get("openrouter_api_key") or
                          os.environ.get("OPENROUTER_API_KEY",""))
                if not or_key:
                    st.error("❌ OpenRouter key Settings mein daalo!")
                elif not pexels_key:
                    st.error("❌ Pexels key Settings mein daalo!")
                else:
                    defs = {"gen_step":0,"gen_script":"","gen_voice":"",
                            "gen_vtt":None,"gen_clips":[],"gen_output":""}
                    for k,v in defs.items():
                        if k not in st.session_state:
                            st.session_state[k] = v

                    try:
                        prog = st.progress(0)
                        stat = st.empty()
                        timer = st.empty()   # live elapsed / ETA line

                        # Store start time only on fresh run
                        if st.session_state["gen_step"] == 0:
                            st.session_state["gen_start"] = time_module.time()
                            st.session_state["gen_step_times"] = {}

                        gen_start = st.session_state.get("gen_start", time_module.time())

                        # ── Step 1 — Script ──────────────────────────────────
                        if st.session_state["gen_step"] < 1:
                            _s = time_module.time()
                            stat.info("📝 Step 1/4 — Script generate ho rahi hai...")
                            timer.caption(
                                f"⏱️ Elapsed: {_fmt_time(time_module.time()-gen_start)}"
                                f"  |  Est. total: {ETA_MAP.get(video_length,'~3 min')}")
                            if manual_script:
                                st.session_state["gen_script"] = clean_script(manual_script)
                            else:
                                st.session_state["gen_script"] = generate_script_api(
                                    video_subject, video_length, video_language, cfg)
                            st.session_state["gen_step"] = 1
                            st.session_state["gen_step_times"]["script"] = time_module.time()-_s
                        prog.progress(25)
                        _t1 = st.session_state["gen_step_times"].get("script", 0)
                        stat.success(
                            f"✅ Step 1 — Script ready!"
                            f"  ({len(st.session_state['gen_script'].split())} words)"
                            f"  · ⏱️ {_fmt_time(_t1)}")
                        timer.caption(
                            f"⏱️ Elapsed: {_fmt_time(time_module.time()-gen_start)}"
                            f"  |  3 steps remaining")

                        # ── Step 2 — Voice ───────────────────────────────────
                        if st.session_state["gen_step"] < 2:
                            _s = time_module.time()
                            stat.info("🎙️ Step 2/4 — Voice generate ho rahi hai...")
                            timer.caption(
                                f"⏱️ Elapsed: {_fmt_time(time_module.time()-gen_start)}"
                                f"  |  2 heavy steps remaining")
                            if tts_engine == "ElevenLabs 🎭 (Emotional)":
                                stat.info("🎭 Step 2/4 — ElevenLabs emotional voice generate ho rahi hai...")
                                vp, vtt = generate_voice_elevenlabs(
                                    st.session_state["gen_script"],
                                    el_voice_id, el_stability, el_style, cfg)
                                if not vp:
                                    stat.error("❌ ElevenLabs failed — Edge-TTS pe fallback...")
                                    vp, vtt = generate_voice(
                                        st.session_state["gen_script"],
                                        selected_voice, voice_speed, voice_deep)
                            else:
                                vp, vtt = generate_voice(
                                    st.session_state["gen_script"],
                                    selected_voice, voice_speed, voice_deep)
                            st.session_state["gen_voice"] = vp
                            st.session_state["gen_vtt"]   = vtt
                            st.session_state["gen_step"]  = 2
                            st.session_state["gen_step_times"]["voice"] = time_module.time()-_s
                        prog.progress(50)
                        _t2 = st.session_state["gen_step_times"].get("voice", 0)
                        stat.success(
                            f"✅ Step 2 — Voice + Subtitles ready!"
                            f"  · ⏱️ {_fmt_time(_t2)}")
                        timer.caption(
                            f"⏱️ Elapsed: {_fmt_time(time_module.time()-gen_start)}"
                            f"  |  2 steps remaining")

                        # ── Step 3 — Clips ───────────────────────────────────
                        if st.session_state["gen_step"] < 3:
                            _s = time_module.time()
                            stat.info("🖼️ Step 3/4 — Video clips fetch ho rahe hain...")
                            timer.caption(
                                f"⏱️ Elapsed: {_fmt_time(time_module.time()-gen_start)}"
                                f"  |  Compose step aane wali hai")
                            kw = video_subject or \
                                " ".join(st.session_state["gen_script"].split()[:4])
                            items = fetch_pexels(
                                kw, 6, cfg,
                                media_type=pexels_media,
                                vibe=video_vibe)
                            if not items:
                                st.warning("⚠️ Pexels media nahi mili — topic/vibe change karo!")
                                st.stop()
                            cdur_est = (DURATION_SECS.get(video_length,30)
                                        / max(len(items[:6]),1))
                            clips = [download_clip_or_photo(item, i, cdur_est)
                                     for i, item in enumerate(items[:6])]
                            clips = [c for c in clips if c and os.path.exists(c)]
                            st.session_state["gen_clips"] = clips
                            st.session_state["gen_step"]  = 3
                            st.session_state["gen_step_times"]["clips"] = time_module.time()-_s
                        prog.progress(75)
                        _t3 = st.session_state["gen_step_times"].get("clips", 0)
                        stat.success(
                            f"✅ Step 3 — {len(st.session_state['gen_clips'])} clips ready!"
                            f"  · ⏱️ {_fmt_time(_t3)}")

                        # ── Step 4 — Compose (longest) ───────────────────────
                        if st.session_state["gen_step"] < 4:
                            _s = time_module.time()
                            _c_eta = COMPOSE_ETA.get(video_length, 150)
                            stat.info(
                                f"🎬 Step 4/4 — Video compose ho rahi hai..."
                                f"  (est. {_fmt_time(_c_eta)})  ⏳ Page band mat karo!")
                            timer.warning(
                                f"⏱️ Elapsed: {_fmt_time(time_module.time()-gen_start)}"
                                f"  |  🎬 Compose step — est. {_fmt_time(_c_eta)} lagega")
                            out = compose_video(
                                st.session_state["gen_clips"],
                                st.session_state["gen_voice"],
                                st.session_state["gen_vtt"],
                                video_aspect, font_size,
                                subtitle_color, subtitle_position,
                                st.session_state["gen_script"],
                                bg_music, bg_volume,
                                max_dur_secs=DURATION_SECS.get(video_length),
                                language=video_language)
                            st.session_state["gen_output"] = out
                            st.session_state["gen_step"]   = 4
                            st.session_state["gen_step_times"]["compose"] = time_module.time()-_s
                        prog.progress(100)

                        _total = time_module.time() - gen_start
                        _st    = st.session_state["gen_step_times"]
                        final_dur = get_duration(st.session_state["gen_output"])
                        stat.success(
                            f"🎉 Video ban gayi!  Length: {final_dur:.1f}s  "
                            f"· Total time: {_fmt_time(_total)}")
                        timer.success(
                            f"📊 Breakdown — "
                            f"Script: {_fmt_time(_st.get('script',0))}  "
                            f"Voice: {_fmt_time(_st.get('voice',0))}  "
                            f"Clips: {_fmt_time(_st.get('clips',0))}  "
                            f"Compose: {_fmt_time(_st.get('compose',0))}")

                        with st.expander("📝 Generated Script dekho"):
                            st.write(st.session_state["gen_script"])

                        if os.path.exists(st.session_state["gen_output"]):
                            with open(st.session_state["gen_output"],"rb") as f:
                                vbytes = f.read()
                            st.video(vbytes)
                            st.download_button(
                                "⬇️ Video Download Karo",
                                data=vbytes,
                                file_name=f"brainreel_{video_subject[:20]}.mp4",
                                mime="video/mp4",
                                use_container_width=True)

                    except Exception as e:
                        st.error(f"❌ Error: {str(e)}")
                        st.warning(
                            "💡 Connecting aaya tha? "
                            "Dobara Generate dabao — wahan se continue hoga!")

elif page == "⚙️  Settings":
    st.markdown("## ⚙️ Settings")
    st.caption("API keys aur models configure karo.")
    s1, s2 = st.tabs(["🔑 API Keys","🤖 Models"])

    with s1:
        st.markdown('<div class="card-title">🔑 API Keys</div>',
                    unsafe_allow_html=True)
        for label, key, ph in [
            ("OpenAI API Key","openai_api_key","sk-..."),
            ("OpenRouter API Key","openrouter_api_key","sk-or-..."),
            ("Pexels API Key","pexels_api_key","Pexels key"),
            ("Pixabay API Key (Optional)","pixabay_api_key","Pixabay key"),
            ("ElevenLabs API Key — Real Emotions 🎭 (Optional)","elevenlabs_api_key","el-..."),
        ]:
            val = st.text_input(label, value=cfg.get(key,""),
                                type="password", placeholder=ph, key=f"inp_{key}")
            st.markdown(
                f'<span class="key-status {"key-set" if cfg.get(key) else "key-empty"}">'
                f'{"✓ Set hai" if cfg.get(key) else "○ Empty"}</span>',
                unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("💾 Save API Keys"):
            for _, key, __ in [
                ("","openai_api_key",""),
                ("","openrouter_api_key",""),
                ("","pexels_api_key",""),
                ("","pixabay_api_key",""),
                ("","elevenlabs_api_key",""),
            ]:
                cfg[key] = st.session_state.get(f"inp_{key}","")
            if save_config(cfg):
                st.success("✅ Keys save ho gayi!")
                st.rerun()
            else:
                st.error("❌ Save nahi hua!")

    with s2:
        st.markdown('<div class="card-title">🤖 Model Settings</div>',
                    unsafe_allow_html=True)
        providers = ["OpenAI","OpenRouter","DeepSeek",
                     "Moonshot","Google Gemini","Ollama"]
        curr = cfg.get("llm_provider","OpenAI")
        pidx = providers.index(curr) if curr in providers else 0
        llm_provider = st.selectbox("LLM Provider", providers, index=pidx)

        opts = MODEL_LISTS.get(llm_provider, ["Custom (type below)"])
        saved = cfg.get("model_name","")
        didx = opts.index(saved) if saved in opts else len(opts)-1
        sel = st.selectbox("Model (List se chuno)", opts, index=didx)

        if sel == "Custom (type below)":
            model_name = st.text_input("Ya khud likho",
                value=saved if saved not in opts else "",
                placeholder="koi bhi model")
        else:
            model_name = sel

        base_url = st.text_input("Base URL (optional)",
            value=cfg.get("base_url",""),
            placeholder="Default ke liye khali chhodo")

        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("💾 Save Model Settings"):
            cfg.update({"llm_provider":llm_provider,
                        "model_name":model_name,"base_url":base_url})
            if save_config(cfg):
                st.success("✅ Saved!")
                st.rerun()
            else:
                st.error("❌ Save nahi hua!")
