# Video Maker IA

Génère des vidéos (voix off, sous-titres, images Chibi / Cinématographique / Hyper-réaliste, formats 16:9 et 9:16)
à partir de ton script. Deux façons de l'utiliser :

1. **Depuis Claude** (serveur MCP) : tu discutes, Claude pilote l'appli.
2. **Interface web** (Streamlit) : une page avec des boutons.

## Fichiers
- `core.py` : tout le pipeline (découpage, images, voix, sous-titres, montage)
- `mcp_server.py` : serveur MCP pour Claude
- `app.py` : interface web
- `Dockerfile`, `start.sh` : déploiement

## Clés nécessaires
- `REPLICATE_API_TOKEN` : génération des images (replicate.com). Fixe une limite de dépense mensuelle dans ton compte.
- `ANTHROPIC_API_KEY` : seulement pour l'interface web (rédaction des prompts d'image). Pas nécessaire avec Claude + MCP.
- `MCP_SECRET` : 16 caractères minimum, fait partie de l'URL du connecteur. Traite-le comme un mot de passe.
- `PUBLIC_URL` : l'adresse publique de ton service, pour les liens de téléchargement.
- `APP_PASSWORD` : protège l'interface web.

## Test en local
```
pip install -r requirements.txt
export REPLICATE_API_TOKEN=... ANTHROPIC_API_KEY=... APP_PASSWORD=...
streamlit run app.py
```
Serveur MCP en local :
```
export REPLICATE_API_TOKEN=... MCP_SECRET=une-longue-chaine-secrete PUBLIC_URL=http://localhost:8000
python mcp_server.py
```

## Publication (Render, Railway, Fly.io ou un VPS)
1. Mets le dossier sur GitHub (le `.env` n'est pas envoyé).
2. Crée un service web à partir du `Dockerfile`.
3. Ajoute les variables d'environnement ci-dessus.
4. Prévois au moins 2 Go de mémoire : le montage vidéo est gourmand. Un plan gratuit est souvent trop juste.
5. Pour l'interface web, crée un **second** service avec la même image et la variable `APP_MODE=ui`.

Le stockage des vidéos est temporaire sur la plupart des hébergeurs : télécharge-les après génération.

## Connecter Claude
1. Déploie le service MCP (sans `APP_MODE`).
2. Dans Claude, ouvre les réglages, section Connecteurs, et ajoute un connecteur personnalisé.
3. URL à coller : `https://TON-SERVICE/MCP_SECRET/mcp` (remplace par ton adresse et ta valeur de `MCP_SECRET`).
4. Active le connecteur dans la conversation.

Exemple de demande à Claude :
> Voici mon script : [colle le texte]. Style cinématographique, voix française, formats 16:9 et 9:16.

Claude découpe le script, écrit les prompts d'image, te donne le coût estimé, attend ton accord, puis lance la génération
et te donne les liens de téléchargement.

## Outils exposés à Claude
`create_project`, `set_image_prompts`, `update_scene`, `estimate_cost`, `start_generation`, `get_status`.
`start_generation` refuse de démarrer sans `confirm=true`, pour éviter une dépense par erreur.

## Limites connues
- Une seule génération à la fois.
- Le nom du modèle d'animation (`I2V_MODEL` dans `core.py`) et les prix des images sont à vérifier sur Replicate.
- Le même visage d'une scène à l'autre est fiable en Chibi, moins en Hyper-réaliste.
- Polices : DejaVu est installée dans l'image Docker (accents européens). Pour l'arabe ou le chinois, ajoute une police adaptée.
