"""
Interface web (Streamlit) du générateur de vidéos IA.
Lancer :  streamlit run app.py
Variables : ANTHROPIC_API_KEY, REPLICATE_API_TOKEN, APP_PASSWORD (recommandé si l'appli est publique)
"""
import hmac
import os
import tempfile
from pathlib import Path

import streamlit as st

import core

st.set_page_config(page_title="Video Maker IA", page_icon="🎬")
st.title("🎬 Video Maker IA")

# ---- Accès protégé (évite que n'importe qui consomme tes crédits)
pwd = os.getenv("APP_PASSWORD")
if pwd:
    typed = st.text_input("Mot de passe", type="password")
    if not typed or not hmac.compare_digest(typed, pwd):
        st.stop()

missing = [k for k in ("ANTHROPIC_API_KEY", "REPLICATE_API_TOKEN") if not os.getenv(k)]
if missing:
    st.error("Clés manquantes : " + ", ".join(missing))
    st.stop()

ss = st.session_state
ss.setdefault("ver", 0)
ss.setdefault("scenes", [])
ss.setdefault("character", "")
ss.setdefault("results", {})

QUALITY = {"Rapide et bon marché (flux-schnell)": "schnell", "Meilleure qualité (flux-dev)": "dev"}

with st.sidebar:
    lang = st.selectbox("Langue de la voix", list(core.LANGS))
    style_name = st.selectbox("Style visuel", list(core.STYLES))
    q_label = st.selectbox("Images", list(QUALITY), index=0 if style_name == "Chibi" else 1)
    fmts = st.multiselect("Formats", list(core.FORMATS), default=["9:16"])
    max_chars = st.slider("Taille d'une scène (caractères)", 150, 500, 280, step=10)
    rate = st.slider("Vitesse de la voix (%)", -20, 30, 0)
    use_i2v = st.checkbox("Animer les images avec l'IA (très cher sur un long script)", value=False)

# ---- 1. Script
mode = st.radio("Script", ["Coller mon script", "Écrire à partir d'un sujet"], horizontal=True)
if mode == "Écrire à partir d'un sujet":
    topic = st.text_input("Sujet")
    n_chars = st.slider("Longueur visée (caractères)", 800, 12000, 3000, step=200)
    if st.button("Écrire le script", disabled=not topic):
        with st.spinner("Écriture du script..."):
            ss["script_text"] = core.write_script(topic, core.LANGS[lang][1], n_chars)

text = st.text_area("Texte de la narration (colle ton script ici, sans limite de taille)",
                    height=300, key="script_text")
if text.strip():
    n_est = len(core.split_scenes(text, max_chars))
    st.caption(f"{len(text):,} caractères · environ {n_est} scènes · environ {len(text) / 14 / 60:.1f} min de voix"
               .replace(",", " "))
    e = core.estimate(n_est, fmts or ["9:16"], QUALITY[q_label], use_i2v)
    st.caption(f"Images à générer : {e['images']} · coût approximatif des images : {e['cout_images_usd_approx']} $")

# ---- 2. Découpage + prompts d'image
if st.button("1. Découper en scènes et préparer les images", disabled=not text.strip()):
    with st.spinner("Découpage et rédaction des prompts d'image..."):
        chunks = core.split_scenes(text, max_chars)
        ss.character, prompts = core.make_prompts(chunks, style_name)
        ss.scenes = [{"narration": c, "image_prompt": p} for c, p in zip(chunks, prompts)]
        ss.ver += 1
        ss.results = {}

if ss.scenes:
    ss.character = st.text_input("Personnage principal (en anglais, identique à chaque scène)",
                                 ss.character, key=f"char{ss.ver}")
    st.write(f"{len(ss.scenes)} scènes. Ouvre une scène pour corriger son prompt d'image.")
    for i, sc in enumerate(ss.scenes):
        with st.expander(f"Scène {i + 1} – {sc['narration'][:60]}..."):
            sc["narration"] = st.text_area("Narration", sc["narration"], key=f"n{ss.ver}_{i}")
            sc["image_prompt"] = st.text_area("Image (anglais)", sc["image_prompt"], key=f"p{ss.ver}_{i}")

    # ---- 3. Génération
    if st.button("2. Générer la vidéo", disabled=not fmts):
        bar, status = st.progress(0.0), st.empty()

        def progress(frac, msg):
            bar.progress(frac)
            status.write(msg)

        try:
            paths = core.produce_video(
                ss.scenes, ss.character, style_name, lang, fmts, QUALITY[q_label], rate, use_i2v,
                workdir=tempfile.mkdtemp(), progress=progress, warn=st.warning)
            ss.results = {f: Path(p).read_bytes() for f, p in paths.items()}
        except Exception as ex:
            st.error(f"Erreur pendant la génération : {ex}")

# ---- Résultats (restent affichés après un téléchargement)
for fmt, data in ss.results.items():
    st.success(f"Vidéo {fmt} prête")
    st.video(data)
    st.download_button(f"Télécharger {fmt}", data, file_name=f"video_{fmt.replace(':', 'x')}.mp4",
                       key=f"dl{fmt}")
