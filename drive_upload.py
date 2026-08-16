import os
import glob
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

if not GOOGLE_DRIVE_FOLDER_ID:
    raise RuntimeError("❌ GOOGLE_DRIVE_FOLDER_ID n'est pas défini.")

if not GOOGLE_SERVICE_ACCOUNT_JSON:
    raise RuntimeError("❌ GOOGLE_SERVICE_ACCOUNT_JSON n'est pas défini.")


def get_drive_service():
    """
    Construit le client Drive à partir du JSON du compte de service.
    GOOGLE_SERVICE_ACCOUNT_JSON contient le CONTENU du fichier JSON
    (pas un chemin de fichier), pour rester compatible avec les secrets
    GitHub Actions (qui sont des chaînes de texte).
    """
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=SCOPES
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

    files.sort()  # le nom contient la date/heure, donc l'ordre alphabétique = ordre chronologique
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