"""
Pipeline vidéo : script -> scènes -> images -> voix off -> sous-titres -> MP4.
Utilisé à la fois par l'interface web (app.py) et par le serveur MCP (mcp_server.py).
"""
import asyncio
import json
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ----------------------------- CONFIG ---------------------------------
CLAUDE_MODEL = "claude-sonnet-5-5"
I2V_MODEL = "wan-video/wan-2.2-i2v-fast"  # image -> clip ; vérifie le nom et les champs sur replicate.com

IMAGE_MODELS = {
    "schnell": "black-forest-labs/flux-schnell",  # rapide, bon marché
    "dev": "black-forest-labs/flux-dev",          # meilleure qualité
}
APPROX_USD_PER_IMAGE = {"schnell": 0.003, "dev": 0.025}  # ordre de grandeur, à vérifier sur Replicate

STYLES = {
    "Chibi": ("chibi style, cute big-head small-body characters, kawaii, vibrant colors, "
              "clean cel-shaded illustration, soft lighting, detailed background"),
    "Cinématographique": ("cinematic film still, anamorphic lens, dramatic lighting, shallow depth of field, "
                          "rich color grading, subtle film grain, epic composition"),
    "Hyper-réaliste": ("hyper-realistic photograph, shot on 85mm lens, natural lighting, "
                       "ultra detailed textures, sharp focus, photorealistic, 8k"),
}
NO_TEXT = "no text, no letters, no watermark"

LANGS = {  # nom : (voix edge-tts, nom de la langue pour Claude)
    "Français": ("fr-FR-DeniseNeural", "French"),
    "English": ("en-US-JennyNeural", "English"),
    "Español": ("es-ES-ElviraNeural", "Spanish"),
    "Português": ("pt-BR-FranciscaNeural", "Portuguese"),
    "Deutsch": ("de-DE-KatjaNeural", "German"),
    "Italiano": ("it-IT-ElsaNeural", "Italian"),
}

FORMATS = {  # nom : (largeur, hauteur, ratio Flux)
    "16:9": (1280, 720, "16:9"),
    "9:16": (720, 1280, "9:16"),
}

FONT_CANDIDATES = [
    "arialbd.ttf", "Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
]


def pick(options: dict, value: str, label: str) -> str:
    """Retrouve une clé sans tenir compte de la casse, ou explique les choix possibles."""
    for k in options:
        if k.lower() == str(value).lower():
            return k
    raise ValueError(f"{label} inconnu : {value!r}. Choix possibles : {', '.join(options)}")


# ----------------------------- TEXTE ----------------------------------
def write_script(topic: str, lang_name: str, n_chars: int) -> str:
    from anthropic import Anthropic
    prompt = (f"Écris un script de narration pour une vidéo, d'environ {n_chars} caractères, en {lang_name}.\n"
              f"Sujet : {topic}\n"
              "Ton oral, phrases courtes, début accrocheur, conclusion claire. "
              "Réponds UNIQUEMENT avec le texte à lire à voix haute : pas de titres, pas de didascalies, "
              "pas de markdown.")
    r = Anthropic().messages.create(model=CLAUDE_MODEL, max_tokens=8000,
                                    messages=[{"role": "user", "content": prompt}])
    return r.content[0].text.strip()


def split_scenes(text: str, max_chars: int) -> list[str]:
    """Découpe le script en scènes sans modifier le texte (regroupe des phrases entières)."""
    sentences = []
    for para in re.split(r"\n\s*\n", text.strip()):
        para = " ".join(para.split())
        if para:
            sentences += re.split(r"(?<=[.!?…])\s+", para)
    scenes, cur = [], ""
    for s in sentences:
        if cur and len(cur) + 1 + len(s) > max_chars:
            scenes.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        scenes.append(cur)
    return scenes


def make_prompts(chunks: list[str], style_name: str) -> tuple[str, list[str]]:
    """Demande à Claude (API) un prompt d'image en anglais par scène, avec un personnage cohérent."""
    from anthropic import Anthropic
    client = Anthropic()
    character, prompts, batch_size = "", [], 40
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(batch))
        hint = (f"Personnage principal déjà défini (garde-le tel quel) : {character}"
                if character else "Définis aussi le personnage principal (apparence précise, en anglais).")
        prompt = f"""Tu prépares les images d'une vidéo à partir d'un script découpé en scènes.
Style visuel : {style_name} (il est ajouté automatiquement, ne le décris pas).
{hint}
Pour chaque scène numérotée, écris un prompt d'image en anglais (1 à 2 phrases) : sujet, action, décor, émotion, lumière.
Reste concret et visuel ; pour une idée abstraite, utilise une métaphore visuelle. Aucun texte dans l'image.
Réponds UNIQUEMENT avec du JSON : {{"character": "...", "prompts": ["...", "..."]}} avec exactement {len(batch)} prompts.

Scènes :
{numbered}"""
        r = client.messages.create(model=CLAUDE_MODEL, max_tokens=6000,
                                   messages=[{"role": "user", "content": prompt}])
        data = json.loads(re.sub(r"```json|```", "", r.content[0].text).strip())
        character = character or data.get("character", "")
        got = data["prompts"][:len(batch)]
        got += [batch[len(got) + k] for k in range(len(batch) - len(got))]  # secours si des prompts manquent
        prompts += got
    return character, prompts


# ----------------------------- COÛTS ----------------------------------
def estimate(n_scenes: int, formats: list[str], image_quality: str = "schnell", use_animation: bool = False) -> dict:
    n_img = n_scenes * len(formats)
    out = {
        "scenes": n_scenes,
        "formats": formats,
        "voix_off": n_scenes,
        "images": n_img,
        "cout_images_usd_approx": round(n_img * APPROX_USD_PER_IMAGE[image_quality], 2),
        "animations_ia": n_img if use_animation else 0,
        "note": "Prix indicatifs, à vérifier sur Replicate. Les animations IA coûtent bien plus cher que les images.",
    }
    return out


# ----------------------------- IMAGES / CLIPS --------------------------
def gen_image(prompt: str, ratio: str, path: str, model: str):
    import replicate
    last = None
    for attempt in range(3):
        try:
            out = replicate.run(model, input={"prompt": prompt, "aspect_ratio": ratio,
                                              "num_outputs": 1, "output_format": "png"})
            Path(path).write_bytes(out[0].read())
            return
        except Exception as e:  # limites de débit, erreurs réseau...
            last = e
            time.sleep(4 * (attempt + 1))
    raise last


def animate(img_path: str, prompt: str, out_path: str):
    import replicate
    with open(img_path, "rb") as f:
        out = replicate.run(I2V_MODEL, input={"image": f, "prompt": prompt})
    Path(out_path).write_bytes(out.read())


# ----------------------------- VOIX -----------------------------------
async def _tts(text, voice, path, rate):
    import edge_tts
    await edge_tts.Communicate(text, voice, rate=rate).save(path)


def tts(text: str, voice: str, path: str, rate: str = "+0%"):
    asyncio.run(_tts(text, voice, path, rate))


# ----------------------------- SOUS-TITRES -----------------------------
def get_font(size):
    from PIL import ImageFont
    for f in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def subtitle_array(text, W, H):
    import numpy as np
    from PIL import Image, ImageDraw
    vertical = H > W
    font = get_font(int(W * (0.065 if vertical else 0.045)))
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    max_w = W * 0.86
    lines, cur = [], ""
    for w in text.split():
        test = (cur + " " + w).strip()
        if d.textlength(test, font=font) <= max_w:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    lh = int(getattr(font, "size", 20) * 1.25)
    y = int(H * (0.72 if vertical else 0.82)) - (len(lines) - 1) * lh // 2
    for line in lines:
        x = (W - d.textlength(line, font=font)) / 2
        d.text((x, y), line, font=font, fill="white", stroke_width=max(3, W // 250), stroke_fill="black")
        y += lh
    return np.array(img)


def subtitle_clips(text, duration, W, H):
    from moviepy import ImageClip
    words = text.split()
    n = 4 if H > W else 7
    chunks = [" ".join(words[i:i + n]) for i in range(0, len(words), n)] or [text]
    total = sum(len(c) for c in chunks)
    clips, t = [], 0.0
    for c in chunks:
        dur = duration * len(c) / total
        clips.append(ImageClip(subtitle_array(c, W, H)).with_start(t).with_duration(dur))
        t += dur
    return clips


# ----------------------------- MONTAGE ---------------------------------
def fill(clip, W, H):
    scale = max(W / clip.w, H / clip.h)
    r = clip.resized(scale)
    return r.cropped(width=W, height=H, x_center=r.w / 2, y_center=r.h / 2)


def render_scene(img_path, vid_path, audio_path, text, W, H, out_path):
    """Monte une scène et l'écrit sur disque (évite de tout garder en mémoire pour les longues vidéos)."""
    from moviepy import AudioFileClip, CompositeVideoClip, ImageClip, VideoFileClip, vfx
    audio = AudioFileClip(audio_path)
    d = audio.duration + 0.3
    if vid_path:
        base = VideoFileClip(vid_path).without_audio()
        base = base.with_effects([vfx.MultiplySpeed(final_duration=d)])
        base = fill(base, W, H)
    else:  # zoom lent (Ken Burns)
        c = fill(ImageClip(img_path).with_duration(d), W, H)
        zoom = c.resized(lambda t: 1 + 0.08 * t / d)
        base = CompositeVideoClip([zoom.with_position("center")], size=(W, H))
    comp = CompositeVideoClip([base] + subtitle_clips(text, d, W, H), size=(W, H))
    comp = comp.with_duration(d).with_audio(audio)
    comp.write_videofile(out_path, fps=24, codec="libx264", audio_codec="aac",
                         preset="veryfast", logger=None)
    comp.close()
    audio.close()


def concat(files: list[str], out: str):
    import imageio_ffmpeg
    lst = out + ".txt"
    Path(lst).write_text("".join(f"file '{Path(f).as_posix()}'\n" for f in files), encoding="utf-8")
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0",
                    "-i", lst, "-c", "copy", out], check=True, capture_output=True)


# ----------------------------- PIPELINE COMPLET ------------------------
def produce_video(scenes, character, style, lang, formats, image_quality="schnell",
                  rate=0, use_animation=False, workdir=None, progress=lambda frac, msg: None,
                  warn=lambda msg: None) -> dict:
    """
    scenes : liste de {"narration": str, "image_prompt": str}
    Retourne {format: chemin du MP4}.
    """
    work = Path(workdir or tempfile.mkdtemp())
    work.mkdir(parents=True, exist_ok=True)
    voice, rate_str = LANGS[lang][0], f"{rate:+d}%"
    model = IMAGE_MODELS[image_quality]
    n = len(scenes)
    total = n * (1 + 2 * len(formats))
    state = {"done": 0}

    def tick(msg):
        state["done"] += 1
        progress(min(state["done"] / total, 1.0), msg)

    audios = []
    for i, sc in enumerate(scenes):
        p = str(work / f"a{i}.mp3")
        tts(sc["narration"], voice, p, rate_str)
        audios.append(p)
        tick(f"Voix off {i + 1}/{n}")

    results = {}
    for fmt in formats:
        W, H, ratio = FORMATS[fmt]
        tag = fmt.replace(":", "x")
        imgs = [str(work / f"i{i}_{tag}.png") for i in range(n)]
        with ThreadPoolExecutor(max_workers=3) as ex:
            jobs = [ex.submit(gen_image,
                              f"{character}. {sc['image_prompt']}. {STYLES[style]}. {NO_TEXT}",
                              ratio, imgs[i], model)
                    for i, sc in enumerate(scenes)]
            for k, f in enumerate(as_completed(jobs)):
                f.result()
                tick(f"{fmt} : images {k + 1}/{n}")

        files = []
        for i, sc in enumerate(scenes):
            vid = None
            if use_animation:
                try:
                    vid = str(work / f"v{i}_{tag}.mp4")
                    animate(imgs[i], sc["image_prompt"], vid)
                except Exception as e:
                    warn(f"Animation échouée (scène {i + 1}), zoom utilisé : {e}")
                    vid = None
            out = str(work / f"s{i}_{tag}.mp4")
            render_scene(imgs[i], vid, audios[i], sc["narration"], W, H, out)
            files.append(out)
            tick(f"{fmt} : montage {i + 1}/{n}")

        final = str(work / f"video_{tag}.mp4")
        concat(files, final)
        results[fmt] = final
    progress(1.0, "Terminé")
    return results
