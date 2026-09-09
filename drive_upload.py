import os
import glob
import json
from datetime import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")

REQUIRED_VARS = {
    "GOOGLE_DRIVE_FOLDER_ID": GOOGLE_DRIVE_FOLDER_ID,
    "GOOGLE_OAUTH_CLIENT_ID": GOOGLE_OAUTH_CLIENT_ID,
    "GOOGLE_OAUTH_CLIENT_SECRET": GOOGLE_OAUTH_CLIENT_SECRET,
    "GOOGLE_OAUTH_REFRESH_TOKEN": GOOGLE_OAUTH_REFRESH_TOKEN,
}

for name, value in REQUIRED_VARS.items():
    if not value:
        raise RuntimeError(f"❌ {name} n'est pas défini.")


def get_drive_service():
    """
    Construit le client Drive à partir des identifiants OAuth du compte
    .org dédié (ex: automation-sociavault@polaris-asso.org, ou celui
    utilisé temporairement).

    Le refresh_token a été généré UNE SEULE FOIS en local via
    generate_refresh_token.py, avec l'app OAuth configurée en mode
    "Internal" (donc sans expiration côté Google Workspace).

    google-auth rafraîchit automatiquement le token d'accès à partir
    du refresh_token à chaque appel, pas besoin de le faire à la main.
    """
    credentials = Credentials(
        token=None,
        refresh_token=GOOGLE_OAUTH_REFRESH_TOKEN,
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=credentials)


def find_latest_corpus_file(pattern_name):
    """
    Trouve le fichier le plus récent correspondant au motif donné
    dans data/, en se basant sur le nom (horodaté) du fichier.
    Retourne None si aucun fichier ne correspond (au lieu de lever une
    erreur) : avec plusieurs sources à fusionner, l'absence de l'une
    d'elles ne doit pas empêcher de traiter les autres.
    """
    pattern = os.path.join(DATA_DIR, pattern_name)
    files = glob.glob(pattern)
    if not files:
        return None
    files.sort()
    return files[-1]


def merge_corpus_files(filepaths):
    """
    Fusionne plusieurs fichiers corpus (chacun une liste de 'target' au
    format {target_name, platform, target_url, country_code,
    posts_collectes: [...]}) en une seule liste, et l'écrit dans un
    nouveau fichier horodaté sociavault_raw_complet_{date}.json.

    Ne modifie ni ne supprime les fichiers sources : ils restent
    disponibles individuellement dans data/ si besoin de les inspecter
    separement.
    """
    merged = []
    for path in filepaths:
        with open(path, "r", encoding="utf-8") as f:
            content = json.load(f)
        if not isinstance(content, list):
            print(f"⚠️  {path} n'est pas une liste, ignoré dans la fusion.")
            continue
        merged.extend(content)
        print(f"   + {len(content)} cible(s) ajoutée(s) depuis {os.path.basename(path)}")

    merged_filename = f"sociavault_raw_complet_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
    merged_path = os.path.join(DATA_DIR, merged_filename)
    with open(merged_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    total_comments = sum(
        p.get("comments_count", 0) for t in merged for p in t.get("posts_collectes", [])
    )
    print(f"📦 Fusion : {len(merged)} cible(s) au total, {total_comments} commentaire(s), écrit dans {merged_path}")
    return merged_path


def upload_file_to_drive(filepath, folder_id):
    service = get_drive_service()

    filename = os.path.basename(filepath)

    file_metadata = {
        "name": filename,
        "parents": [folder_id]
    }

    media = MediaFileUpload(filepath, mimetype="application/json", resumable=True)

    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name, webViewLink"
    ).execute()

    print(f"✅ Fichier uploadé : {uploaded.get('name')}")
    print(f"   ID Drive : {uploaded.get('id')}")
    print(f"   Lien : {uploaded.get('webViewLink')}")

    return uploaded


def main():
    sources = [
        ("sociavault_raw*.json", "corpus SociaVault (cibles propres)"),
        ("sociavault_medias_internationaux_raw*.json", "corpus médias internationaux"),
    ]

    found_files = []
    for pattern, label in sources:
        latest_file = find_latest_corpus_file(pattern)
        if not latest_file:
            print(f"⚠️  Aucun fichier trouvé pour {label} (motif {pattern}), on passe.")
            continue
        print(f"📄 Fichier trouvé ({label}) : {latest_file}")
        found_files.append(latest_file)

    if not found_files:
        raise RuntimeError(f"❌ Aucun fichier corpus trouvé dans {DATA_DIR}")

    if len(found_files) == 1:
        # Un seul type de corpus disponible ce run : pas de fusion a faire,
        # on uploade tel quel pour ne pas produire un doublon inutile.
        print("ℹ️  Un seul corpus disponible ce run, upload direct sans fusion.")
        upload_file_to_drive(found_files[0], GOOGLE_DRIVE_FOLDER_ID)
        return

    print("🔀 Fusion des corpus avant upload...")
    merged_path = merge_corpus_files(found_files)
    upload_file_to_drive(merged_path, GOOGLE_DRIVE_FOLDER_ID)


if __name__ == "__main__":
    main()