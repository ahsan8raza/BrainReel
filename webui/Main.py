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
                "user_name":"Creator"}

def save_config(data):
    try:
        with open(CONFIG_PATH,"w") as f:
            json.dump(data,f,indent=2)
        return True
    except: return False

EMOTION_PRESETS = {
    "😐 Neutral":   {"speed": 50, "deep": 50},
    "😢 Sad / Poetic": {"speed": 38, "deep": 72},
    "🔥 Intense":   {"speed": 58, "deep": 60},
    "😌 Calm":      {"speed": 42, "deep": 45},
    "⚡ Energetic":  {"speed": 68, "deep": 40},
    "🎭 Dramatic":  {"speed": 35, "deep": 78},
}

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

def generate_voice(script, voice, speed=50, deep=50):
    clean = clean_script(script)
    ts = int(time_module.time())
    vp = f"/tmp/voice_{ts}.mp3"
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

# ── Urdu/Arabic font for subtitles ───────────────────────────────────────────
URDU_FONT_URL = (
    "https://github.com/googlefonts/urdu-naskh/raw/main/fonts/ttf/"
    "UrdoNaskh-Regular.ttf"
)
def ensure_urdu_font():
    path = "/tmp/UrdoNaskh.ttf"
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    try:
        r = requests.get(
            "https://github.com/google/fonts/raw/main/ofl/"
            "notosansarabic/NotoSansArabic-Regular.ttf",
            timeout=20)
        if r.status_code == 200:
            open(path,"wb").write(r.content)
            return path
    except Exception:
        pass
    return None

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

    if words:
        with open(srt,'w',encoding='utf-8') as f:
            for i,(s,e,w) in enumerate(words,1):
                f.write(f"{i}\n{sec_to_srt(s)} --> {sec_to_srt(e)}\n{w}\n\n")
    else:
        clean = clean_script(script)
        all_words = clean.split()
        chunk_size = 4
        chunks = [' '.join(all_words[j:j+chunk_size])
                  for j in range(0, len(all_words), chunk_size)]
        tpc = dur / max(len(chunks), 1)
        with open(srt,'w',encoding='utf-8') as f:
            for i, ch in enumerate(chunks, 1):
                s2, e2 = i*tpc-tpc, i*tpc
                f.write(f"{i}\n{sec_to_srt(s2)} --> {sec_to_srt(e2)}\n{ch}\n\n")

    # RTL languages need right-align; also download special font for Urdu/Arabic
    _rtl_langs = {"Urdu", "Arabic"}
    _align_override = 6 if language in _rtl_langs else align

    _font_name = "NotoSansArabic"
    _font_extra = ""
    if language in _rtl_langs:
        _ufont = ensure_urdu_font()
        if _ufont:
            _font_extra = f",Fontname={_font_name}"

    sub_f = (f"subtitles={srt}:force_style='"
             f"FontSize={font_size},"
             f"PrimaryColour={ass_c},"
             f"OutlineColour=&H00000000&,"
             f"BackColour=&H80000000&,"
             f"Bold=1,Outline=2,Shadow=1,"
             f"MarginV=50,"
             f"MaxLines=1,"
             f"Alignment={_align_override}"
             f"{_font_extra}'")
    # For Urdu, also specify fontsdir so libass can find the font
    if language in _rtl_langs and ensure_urdu_font():
        sub_f += ":fontsdir=/tmp"

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
            
