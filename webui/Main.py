import streamlit as st
import os
import asyncio
import tempfile
import nest_asyncio

nest_asyncio.apply()

# video_subject default — FIX 1
video_subject = ""

st.set_page_config(
    page_title="BrainReel — AI Video Generator",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
    background-color: #0e0e0e;
    color: #f0f0f0;
}
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: visible !important;}
[data-testid="stAppDeployButton"] { display: none !important; }

button[data-testid="collapsedControl"] {
    visibility: visible !important;
    display: flex !important;
    opacity: 1 !important;
    background: linear-gradient(135deg, #7c3aed, #2563eb) !important;
    border-radius: 8px !important;
    border: none !important;
    width: 40px !important;
    height: 40px !important;
    box-shadow: 0 4px 15px rgba(124,58,237,0.5) !important;
}
button[data-testid="collapsedControl"] svg {
    fill: white !important;
}
button[data-testid="expandedControl"] {
    visibility: visible !important;
    display: flex !important;
    opacity: 1 !important;
    background: rgba(255, 255, 255, 0.1) !important;
    border: 1px solid rgba(255, 255, 255, 0.2) !important;
    border-radius: 8px !important;
    width: 36px !important;
    height: 36px !important;
    transition: background 0.2s ease !important;
}
button[data-testid="expandedControl"]:hover {
    background: rgba(255, 255, 255, 0.2) !important;
}
button[data-testid="expandedControl"] svg {
    fill: #f0f0f0 !important;
}
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #1a1a2e 0%, #16213e 100%);
    border-right: 1px solid #2a2a4a;
}
.brand-logo { text-align: center; padding: 20px 0 10px 0; }
.brand-title {
    font-size: 28px; font-weight: 700;
    background: linear-gradient(90deg, #a78bfa, #60a5fa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin: 0;
}
.brand-sub { font-size: 12px; color: #888; margin-top: 4px; }
.card-title {
    font-size: 14px; font-weight: 600; color: #a78bfa;
    margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px;
}
.stButton > button {
    background: linear-gradient(90deg, #7c3aed, #2563eb);
    color: white; border: none; border-radius: 8px;
    font-weight: 600; padding: 10px 24px;
    width: 100%; transition: opacity 0.2s;
}
.stButton > button:hover { opacity: 0.85; color: white; }
.stTextInput > div > div > input,
.stTextArea > div > div > textarea,
.stSelectbox > div > div {
    background: #0e0e1a !important;
    border: 1px solid #2a2a4a !important;
    border-radius: 8px !important;
    color: #f0f0f0 !important;
}
.stTabs [data-baseweb="tab-list"] {
    background: #1a1a2e; border-radius: 10px; padding: 4px; gap: 4px;
}
.stTabs [data-baseweb="tab"] {
    background: transparent; border-radius: 8px;
    color: #888; font-weight: 600; padding: 8px 16px;
}
.stTabs [aria-selected="true"] {
    background: linear-gradient(90deg, #7c3aed, #2563eb) !important;
    color: white !important;
}
.badge {
    display: inline-block; padding: 3px 10px;
    border-radius: 20px; font-size: 11px; font-weight: 600;
}
.badge-ready { background: #064e3b; color: #34d399; }
.badge-warn  { background: #451a03; color: #fb923c; }
.section-header {
    font-size: 11px; text-transform: uppercase;
    letter-spacing: 2px; color: #555; margin: 20px 0 8px 0;
}
</style>
""", unsafe_allow_html=True)

VOICE_MAP = {
    "en-US-AriaNeural — English Female 🇺🇸": "en-US-AriaNeural",
    "en-US-GuyNeural — English Male 🇺🇸": "en-US-GuyNeural",
    "ur-PK-UzmaNeural — Urdu Female 🇵🇰": "ur-PK-UzmaNeural",
    "ur-PK-AsadNeural — Urdu Male 🇵🇰": "ur-PK-AsadNeural",
    "hi-IN-SwaraNeural — Hindi Female 🇮🇳": "hi-IN-SwaraNeural",
    "hi-IN-MadhurNeural — Hindi Male 🇮🇳": "hi-IN-MadhurNeural",
    "ar-SA-ZariyahNeural — Arabic Female 🇸🇦": "ar-SA-ZariyahNeural",
    "zh-CN-XiaoxiaoNeural — Chinese Female 🇨🇳": "zh-CN-XiaoxiaoNeural",
}

PREVIEW_TEXTS = {
    "en-US-AriaNeural": "Hello! I am Aria. I will be the voice of your video.",
    "en-US-GuyNeural": "Hello! I am Guy. I will be the voice of your video.",
    "ur-PK-UzmaNeural": "السلام علیکم! میں آپ کی ویڈیو کی آواز ہوں۔",
    "ur-PK-AsadNeural": "السلام علیکم! میں آپ کی ویڈیو کی آواز ہوں۔",
    "hi-IN-SwaraNeural": "नमस्ते! मैं आपके वीडियो की आवाज़ हूँ।",
    "hi-IN-MadhurNeural": "नमस्ते! मैं आपके वीडियो की आवाज़ हूँ।",
    "ar-SA-ZariyahNeural": "مرحبا! أنا صوت الفيديو الخاص بك.",
    "zh-CN-XiaoxiaoNeural": "你好！我是你视频的声音。",
}

# FIX 2 — asyncio safe version
async def generate_preview(voice_name: str, text: str) -> bytes:
    import edge_tts
    tts = edge_tts.Communicate(text, voice_name)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_path = f.name
    await tts.save(tmp_path)
    with open(tmp_path, "rb") as f:
        audio_bytes = f.read()
    os.unlink(tmp_path)
    return audio_bytes

def run_preview(voice_name: str, text: str) -> bytes:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(generate_preview(voice_name, text))
    finally:
        loop.close()

# ── Sidebar ──
with st.sidebar:
    st.markdown("""
    <div class="brand-logo">
        <div class="brand-title">🎬 BrainReel</div>
        <div class="brand-sub">AI Faceless Video Generator</div>
    </div>
    """, unsafe_allow_html=True)
    st.markdown("---")
    st.markdown('<div class="section-header">Navigation</div>',
                unsafe_allow_html=True)
    page = st.radio(
        label="",
        options=["🎬 Generate Video", "⚙️ Settings",
                 "📜 History", "📊 Analytics"],
        label_visibility="collapsed"
    )
    st.markdown("---")
    st.markdown('<div class="section-header">Status</div>',
                unsafe_allow_html=True)
    st.markdown(
        '<span class="badge badge-warn">⚠ API Not Set</span>',
        unsafe_allow_html=True
    )
    st.markdown("<br>", unsafe_allow_html=True)
    st.caption("v2.0 · BrainReel Edition")

# ── Generate Video ──
if page == "🎬 Generate Video":
    st.markdown("## 🎬 Generate Video")
    st.caption("Fill in the details below and let AI do the magic!")
    tab1, tab2, tab3 = st.tabs(["📝 Script", "🎨 Style", "🔊 Voice & Music"])

    with tab1:
        st.markdown('<div class="card-title">📝 Video Script</div>',
                    unsafe_allow_html=True)
        video_subject = st.text_input("Video Topic / Subject",
            placeholder="e.g. 5 mind-blowing facts about black holes")
        script_mode = st.radio("Script Mode",
            ["🤖 AI Auto Generate", "✍️ Write Manually"], horizontal=True)
        if script_mode == "✍️ Write Manually":
            st.text_area("Your Script",
                         placeholder="Yahan script likho...", height=180)
        col1, col2 = st.columns(2)
        with col1:
            st.selectbox("Language",
                ["English", "Urdu", "Hindi", "Arabic", "Chinese"])
        with col2:
            st.selectbox("Video Length",
                ["30 seconds", "1 minute", "3 minutes", "5 minutes"])

    with tab2:
        st.markdown('<div class="card-title">🎨 Visual Style</div>',
                    unsafe_allow_html=True)
        col1, col2 = st.columns(2)
        with col1:
            st.selectbox("Aspect Ratio",
                ["9:16 (Vertical / TikTok)", "16:9 (Horizontal / YouTube)"])
        with col2:
            st.selectbox("Video Source",
                ["Pexels (Free)", "Pixabay (Free)", "Local Files"])
        st.markdown("**Subtitle Settings**")
        col3, col4 = st.columns(2)
        with col3:
            st.slider("Font Size", 30, 100, 60)
        with col4:
            st.selectbox("Position", ["Bottom", "Center", "Top"])
        st.color_picker("Subtitle Color", "#FFFFFF")

    with tab3:
        st.markdown('<div class="card-title">🔊 Voice & Music</div>',
                    unsafe_allow_html=True)
        voice_display = st.selectbox("TTS Voice", list(VOICE_MAP.keys()))
        selected_voice = VOICE_MAP[voice_display]
        col_prev, col_empty = st.columns([1, 2])
        with col_prev:
            preview_btn = st.button("🔊 Preview Voice")
        if preview_btn:
            with st.spinner("🎙️ Voice generate ho rahi hai..."):
                try:
                    preview_text = PREVIEW_TEXTS.get(
                        selected_voice, "Hello! This is a preview.")
                    # FIX 2 — safe asyncio call
                    audio_bytes = run_preview(selected_voice, preview_text)
                    st.audio(audio_bytes, format="audio/mp3")
                    st.success("✅ Preview ready!")
                except Exception as e:
                    st.error(f"❌ Error: {e}")
        st.slider("Background Music Volume", 0, 100, 30)
        st.selectbox("Background Music",
            ["Random", "Calm", "Upbeat", "Cinematic", "No Music"])

    st.markdown("<br>", unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        if st.button("🚀 Generate Video", use_container_width=True):
            if not video_subject:
                st.error("⚠️ Video topic enter karo pehle!")
            else:
                with st.status("🎬 Generating...", expanded=True) as status:
                    st.write("📝 Script likh raha hai...")
                    st.write("🖼️ Clips fetch ho rahe hain...")
                    st.write("🔊 Voiceover ban raha hai...")
                    st.write("🎬 Video compose ho rahi hai...")
                    status.update(label="✅ Done!",
                                  state="complete", expanded=False)
                st.success("🎉 Video ready!")
                st.info("💡 API keys Settings mein daalo!")

elif page == "⚙️ Settings":
    st.markdown("## ⚙️ Settings")
    st.caption("API keys aur models configure karo.")
    s1, s2 = st.tabs(["🔑 API Keys", "🤖 Models"])
    with s1:
        st.markdown('<div class="card-title">🔑 API Keys</div>',
                    unsafe_allow_html=True)
        st.text_input("OpenAI API Key", type="password", placeholder="sk-...")
        st.text_input("OpenRouter API Key", type="password",
                      placeholder="sk-or-...")
        st.text_input("Pexels API Key", type="password",
                      placeholder="Pexels key")
        st.text_input("Pixabay API Key", type="password",
                      placeholder="Pixabay key")
        if st.button("💾 Save API Keys"):
            st.success("✅ Saved!")
    with s2:
        st.markdown('<div class="card-title">🤖 Model Settings</div>',
                    unsafe_allow_html=True)
        st.selectbox("LLM Provider",
            ["OpenAI", "OpenRouter", "DeepSeek",
             "Moonshot", "Google Gemini", "Ollama"])
        st.text_input("Model Name", placeholder="gpt-4o / deepseek-chat")
        st.text_input("Base URL (optional)",
                      placeholder="Default ke liye khali chhodo")
        if st.button("💾 Save Model Settings"):
            st.success("✅ Saved!")

elif page == "📜 History":
    st.markdown("## 📜 Video History")
    st.info("🎬 Abhi koi video nahi — Generate Video pe jao!")

elif page == "📊 Analytics":
    st.markdown("## 📊 Analytics")
    c1, c2, c3 = st.columns(3)
    with c1: st.metric("Videos Generated", "0")
    with c2: st.metric("Total Duration", "0 min")
    with c3: st.metric("Success Rate", "—")
    st.info("📊 Videos generate hone ke baad stats ayenge!")
