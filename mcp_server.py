"""
Serveur MCP : permet à Claude (claude.ai, app mobile) de piloter le générateur de vidéos.

Variables d'environnement :
  REPLICATE_API_TOKEN  (obligatoire)  génération des images
  MCP_SECRET           (obligatoire)  16 caractères minimum ; fait partie de l'URL, garde-le privé
  PUBLIC_URL           (conseillé)    ex. https://mon-app.onrender.com  (pour les liens de téléchargement)
  DATA_DIR             (optionnel)    dossier de stockage des projets (défaut : ./data)
  PORT                 (optionnel)    défaut 8000

URL à ajouter comme connecteur dans Claude :  {PUBLIC_URL}/{MCP_SECRET}/mcp
"""
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse

import core

SECRET = os.environ.get("MCP_SECRET", "")
if len(SECRET) < 16:
    raise SystemExit("MCP_SECRET manquant ou trop court (16 caractères minimum).")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
DATA = Path(os.environ.get("DATA_DIR", "./data"))
DATA.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("PORT", "8000"))

INSTRUCTIONS = """Générateur de vidéos IA (voix off, sous-titres, images en style Chibi, Cinématographique ou Hyper-réaliste).
Procédure à suivre :
1. create_project avec le script complet de l'utilisateur (le texte n'est jamais modifié).
2. Écris toi-même un prompt d'image en anglais par scène (concret et visuel, sans texte dans l'image) et un
   personnage principal identique partout, puis appelle set_image_prompts. Montre un résumé à l'utilisateur.
3. Appelle estimate_cost et présente le coût à l'utilisateur. Ne lance JAMAIS start_generation sans son accord explicite.
4. start_generation (confirm=true), puis get_status régulièrement jusqu'à l'état 'done'. Donne alors les liens de téléchargement."""

mcp = FastMCP("video-maker-ia", instructions=INSTRUCTIONS, host="0.0.0.0", port=PORT,
              streamable_http_path=f"/{SECRET}/mcp")

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


# ----------------------------- STOCKAGE --------------------------------
def _dir(job_id: str) -> Path:
    return DATA / job_id


def _save(job: dict):
    _dir(job["id"]).mkdir(parents=True, exist_ok=True)
    (_dir(job["id"]) / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")


def _get(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id or ""):
        raise ValueError("job_id invalide")
    if job_id in JOBS:
        return JOBS[job_id]
    p = _dir(job_id) / "job.json"
    if not p.exists():
        raise ValueError("Projet introuvable")
    job = json.loads(p.read_text(encoding="utf-8"))
    if job["state"] == "running":
        job["state"], job["message"] = "error", "Interrompu par un redémarrage du serveur"
    JOBS[job_id] = job
    return job


def _links(job: dict) -> dict:
    return {fmt: f"{PUBLIC_URL}/{SECRET}/files/{job['id']}/{name}" for fmt, name in job.get("files", {}).items()}


# ----------------------------- OUTILS MCP ------------------------------
@mcp.tool()
def create_project(script: str, language: str = "Français", style: str = "Chibi",
                   formats: list[str] | None = None, max_chars: int = 280,
                   image_quality: str = "") -> dict:
    """Crée un projet à partir du script complet (aucune limite de taille) et le découpe en scènes.
    language : Français, English, Español, Português, Deutsch, Italiano (langue de la voix).
    style : Chibi, Cinématographique ou Hyper-réaliste.
    formats : liste parmi "16:9" et "9:16" (défaut ["9:16"]).
    max_chars : taille d'une scène, de 150 à 500 caractères.
    image_quality : "schnell" (rapide, bon marché) ou "dev" (meilleure qualité). Vide = auto selon le style.
    Retourne le job_id et la narration de chaque scène."""
    if not script.strip():
        raise ValueError("Le script est vide.")
    language = core.pick(core.LANGS, language, "Langue")
    style = core.pick(core.STYLES, style, "Style")
    formats = formats or ["9:16"]
    formats = [core.pick(core.FORMATS, f, "Format") for f in formats]
    max_chars = max(150, min(500, int(max_chars)))
    quality = image_quality or ("schnell" if style == "Chibi" else "dev")
    quality = core.pick(core.IMAGE_MODELS, quality, "Qualité d'image")
    chunks = core.split_scenes(script, max_chars)
    job = {
        "id": uuid.uuid4().hex[:12], "state": "draft", "progress": 0.0, "message": "",
        "language": language, "style": style, "formats": formats, "image_quality": quality,
        "character": "", "scenes": [{"narration": c, "image_prompt": ""} for c in chunks], "files": {},
    }
    with LOCK:
        JOBS[job["id"]] = job
        _save(job)
    return {"job_id": job["id"], "n_scenes": len(chunks),
            "scenes": [{"index": i, "narration": c} for i, c in enumerate(chunks)],
            "next_step": "Rédige un prompt d'image anglais par scène et un personnage principal, puis appelle set_image_prompts."}


@mcp.tool()
def set_image_prompts(job_id: str, character: str, prompts: list[str]) -> dict:
    """Enregistre le personnage principal (en anglais) et un prompt d'image anglais par scène.
    Le nombre de prompts doit être égal au nombre de scènes."""
    job = _get(job_id)
    if job["state"] == "running":
        raise ValueError("Génération en cours : modification impossible.")
    if len(prompts) != len(job["scenes"]):
        raise ValueError(f"{len(prompts)} prompts reçus pour {len(job['scenes'])} scènes.")
    job["character"] = character.strip()
    for sc, p in zip(job["scenes"], prompts):
        sc["image_prompt"] = p.strip()
    _save(job)
    return {"ok": True, "n_scenes": len(prompts)}


@mcp.tool()
def update_scene(job_id: str, index: int, narration: str | None = None, image_prompt: str | None = None) -> dict:
    """Modifie la narration et/ou le prompt d'image d'une scène (index à partir de 0)."""
    job = _get(job_id)
    if job["state"] == "running":
        raise ValueError("Génération en cours : modification impossible.")
    if not 0 <= index < len(job["scenes"]):
        raise ValueError("Index de scène invalide.")
    if narration is not None:
        job["scenes"][index]["narration"] = narration
    if image_prompt is not None:
        job["scenes"][index]["image_prompt"] = image_prompt
    _save(job)
    return {"ok": True, "scene": job["scenes"][index]}


@mcp.tool()
def estimate_cost(job_id: str, use_animation: bool = False) -> dict:
    """Estime le nombre d'images et le coût approximatif avant de lancer la génération."""
    job = _get(job_id)
    return core.estimate(len(job["scenes"]), job["formats"], job["image_quality"], use_animation)


@mcp.tool()
def start_generation(job_id: str, confirm: bool = False, use_animation: bool = False,
                     voice_speed_percent: int = 0) -> dict:
    """Lance la génération en arrière-plan. confirm doit être true, et seulement après l'accord explicite
    de l'utilisateur sur le coût. use_animation=true anime chaque image avec l'IA (beaucoup plus cher).
    voice_speed_percent : de -20 à 30."""
    if not confirm:
        raise ValueError("confirm=true requis : présente d'abord estimate_cost à l'utilisateur et attends son accord.")
    job = _get(job_id)
    with LOCK:
        if any(j["state"] == "running" for j in JOBS.values()):
            raise ValueError("Une génération est déjà en cours. Attends qu'elle se termine.")
        if any(not sc["image_prompt"] for sc in job["scenes"]) or not job["character"]:
            raise ValueError("Prompts d'image ou personnage manquants : appelle set_image_prompts d'abord.")
        job.update(state="running", progress=0.0, message="Démarrage", files={})
        _save(job)
    rate = max(-20, min(30, int(voice_speed_percent)))
    threading.Thread(target=_run, args=(job["id"], use_animation, rate), daemon=True).start()
    return {"started": True, "job_id": job["id"], "next_step": "Appelle get_status toutes les 30 à 60 secondes."}


@mcp.tool()
def get_status(job_id: str) -> dict:
    """Retourne l'état (draft, running, done, error), la progression (0 à 1), le message et les liens de téléchargement."""
    job = _get(job_id)
    return {"state": job["state"], "progress": job["progress"], "message": job["message"],
            "n_scenes": len(job["scenes"]), "style": job["style"], "language": job["language"],
            "formats": job["formats"], "download_links": _links(job)}


# ----------------------------- TÂCHE DE FOND ---------------------------
def _run(job_id: str, use_animation: bool, rate: int):
    job = JOBS[job_id]
    work = _dir(job_id) / "work"

    def progress(frac, msg):
        job["progress"], job["message"] = round(frac, 3), msg

    try:
        paths = core.produce_video(job["scenes"], job["character"], job["style"], job["language"],
                                   job["formats"], job["image_quality"], rate, use_animation,
                                   workdir=str(work), progress=progress,
                                   warn=lambda m: job.update(message=m))
        for fmt, p in paths.items():
            name = f"video_{fmt.replace(':', 'x')}.mp4"
            shutil.move(p, _dir(job_id) / name)
            job["files"][fmt] = name
        job.update(state="done", progress=1.0, message="Terminé")
    except Exception as e:
        job.update(state="error", message=f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        _save(job)


# ----------------------------- ROUTES HTTP -----------------------------
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return PlainTextResponse("ok")


@mcp.custom_route(f"/{SECRET}/files/{{job_id}}/{{name}}", methods=["GET"])
async def download(request: Request):
    job_id, name = request.path_params["job_id"], request.path_params["name"]
    if not re.fullmatch(r"[0-9a-f]{12}", job_id) or not re.fullmatch(r"video_(16x9|9x16)\.mp4", name):
        return PlainTextResponse("not found", status_code=404)
    p = _dir(job_id) / name
    if not p.exists():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(p, media_type="video/mp4", filename=name)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
