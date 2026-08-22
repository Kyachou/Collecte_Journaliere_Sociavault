import os
import glob
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


def find_latest_corpus_file():
    """
    Trouve le fichier sociavault_raw*.json le plus récent
    dans data/, en se basant sur le nom (horodaté) du fichier.
    """
    pattern = os.path.join(DATA_DIR, "sociavault_raw*.json")
    files = glob.glob(pattern)

    if not files:
        raise RuntimeError(f"❌ Aucun fichier corpus trouvé dans {DATA_DIR}")

    files.sort()
    return files[-1]


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
    latest_file = find_latest_corpus_file()
    print(f"📄 Fichier à uploader : {latest_file}")
    upload_file_to_drive(latest_file, GOOGLE_DRIVE_FOLDER_ID)


if __name__ == "__main__":
    main()