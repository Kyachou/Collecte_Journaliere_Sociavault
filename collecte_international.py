import os
import re
import json
import time
from datetime import datetime, timezone, timedelta

from collecte import (
    api_get,
    BASE_URL,
    DATA_DIR,
    get_tweet_replies,
    tweet_to_comment_like,
    parse_datetime,
    now_utc,
    SLEEP_BETWEEN_REQUESTS,
    RECHECK_DELAY_HOURS,
)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, "data")
INPUT_FILE = os.path.join(DATA_DIR, "urls_du_jour.json")


PENDING_MEDIAS_FILE = os.path.join(DATA_DIR, "pending_medias_internationaux.json")
OUTPUT_JSON = os.path.join(
    DATA_DIR,
    f"sociavault_medias_internationaux_raw{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json",
)
DEBUG_TIKTOK_RAW = os.path.join(DATA_DIR, "debug_tiktok_raw_response.json")


# ---------------------------------------------------------------------------
# Chargement de la liste d'URLs du jour
# ---------------------------------------------------------------------------

def load_urls_du_jour(path):
    if not os.path.exists(path):
        print(f" Fichier introuvable : {path}")
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def load_pending():
    if not os.path.exists(PENDING_MEDIAS_FILE):
        return []
    try:
        with open(PENDING_MEDIAS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f" Impossible de lire pending_medias_internationaux.json : {e}")
        return []


def save_pending(pending):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PENDING_MEDIAS_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)


def save_corpus(corpus):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)


def pending_key(item):
    return item.get("url")


def _country_code_value(pays):
    """
    Normalise le champ 'pays' (liste) vers ce qu'attend le format collect.py
    (country_code). Un seul pays -> chaine simple, comme pour vos cibles
    propres. Plusieurs pays -> liste conservee telle quelle : a traiter cote
    3A comme un signal transfrontalier (meme logique que cc_secondaires
    deja evoquee). Ne pas fusionner silencieusement en une chaine unique,
    l'info de multi-pays serait perdue.
    """
    if isinstance(pays, list):
        if len(pays) == 1:
            return pays[0]
        return pays
    return pays


def _append_or_merge_target(corpus, target_name, platform, target_url, country_code,
                             detail_key, post_detail, comments, recheck_status):
    """
    Ajoute une entree au corpus dans le MEME format que collect.py :
    {target_name, platform, target_url, country_code, posts_collectes: [...]}.
    Chaque post media a son propre target_url (contrairement a collect.py ou
    une cible/page peut avoir plusieurs posts) : on cree donc un nouveau
    "target" par post plutot que de regrouper par media.
    """
    corpus.append({
        "target_name": target_name,
        "platform": platform,
        "target_url": target_url,
        "country_code": _country_code_value(country_code),
        "posts_collectes": [{
            detail_key: post_detail,
            "comments_count": len(comments),
            "comments": comments,
            "recheck_status": recheck_status,
        }],
    })


# ---------------------------------------------------------------------------
# Extraction d'identifiants depuis les URLs
# ---------------------------------------------------------------------------

TWEET_ID_RE = re.compile(r"status/(\d+)")


def extract_tweet_id(url):
    m = TWEET_ID_RE.search(url or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Collecte TikTok (absente de collect.py, ajoutee ici)
# ---------------------------------------------------------------------------

def tiktok_comment_to_comment_like(c):
    user = c.get("user") or {}
    created = c.get("create_time")
    created_iso = None
    if isinstance(created, (int, float)):
        try:
            created_iso = datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
        except Exception:
            created_iso = None
    return {
        "id": c.get("cid") or c.get("comment_id") or c.get("id"),
        "text": c.get("text"),
        "created_at": created_iso or c.get("created_at"),
        "reply_count": c.get("reply_comment_total", c.get("reply_count", 0)) or 0,
        "like_count": c.get("digg_count", c.get("like_count", 0)) or 0,
        "author": {
            "name": user.get("nickname"),
            "username": user.get("unique_id"),
        },
        # Certaines reponses TikTok embarquent directement les reponses au
        # commentaire (pas d'appel separe necessaire, contrairement a Facebook).
        "replies": [
            tiktok_comment_to_comment_like(r)
            for r in (c.get("reply_comment") or c.get("replies") or [])
        ],
    }


def _dump_debug_once(response_json):
    """Ecrit la toute premiere reponse brute recue pour verification manuelle."""
    if os.path.exists(DEBUG_TIKTOK_RAW):
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DEBUG_TIKTOK_RAW, "w", encoding="utf-8") as f:
        json.dump(response_json, f, ensure_ascii=False, indent=2)
    print(f"       Reponse brute TikTok sauvegardee pour verification : {DEBUG_TIKTOK_RAW}")


def get_all_tiktok_comments(video_url):
    """
    Recupere tous les commentaires d'une video TikTok.

    Endpoint : GET /v1/scrape/tiktok/comments?url=...&cursor=...
    Enveloppe de reponse NON CONFIRMEE avec certitude (voir avertissement en
    tete de fichier) : on tente successivement le format maison SociaVault
    (success/data/has_next_page/cursor, comme Facebook) puis un format plat
    (comments/hasMore/cursor).
    """
    endpoint = f"{BASE_URL}/scrape/tiktok/comments"
    all_comments = []
    seen_ids = set()
    cursor = None
    page = 1
    first_response_logged = False

    while True:
        params = {"url": video_url}
        if cursor is not None:
            params["cursor"] = cursor
        try:
            print(f"       TikTok commentaires page {page}")
            res = api_get(endpoint, params=params, timeout=30)
            if res is None:
                break
            print(f"         ↳ HTTP {res.status_code}")
            if res.status_code != 200:
                print(f"       Erreur TikTok : {res.text[:500]}")
                break

            response = res.json()
            if not first_response_logged:
                _dump_debug_once(response)
                first_response_logged = True

            # Tentative 1 : enveloppe maison SociaVault (comme Facebook)
            if "data" in response and isinstance(response.get("data"), dict):
                data = response["data"]
                comments_data = data.get("comments", [])
                has_next_page = data.get("has_next_page", False)
                next_cursor = data.get("cursor")
            else:
                # Tentative 2 : enveloppe plate (comments / hasMore / cursor)
                data = response
                comments_data = response.get("comments", [])
                has_next_page = response.get("hasMore", False)
                next_cursor = response.get("cursor")

            if isinstance(comments_data, dict):
                comments_batch = list(comments_data.values())
            elif isinstance(comments_data, list):
                comments_batch = comments_data
            else:
                comments_batch = []

            print(f"          {len(comments_batch)} commentaire(s)")
            for c in comments_batch:
                cid = c.get("cid") or c.get("comment_id") or c.get("id")
                if cid and cid in seen_ids:
                    continue
                if cid:
                    seen_ids.add(cid)
                all_comments.append(c)

            if not has_next_page or next_cursor is None:
                print("          Fin des commentaires TikTok")
                break
            cursor = next_cursor
            page += 1
            time.sleep(SLEEP_BETWEEN_REQUESTS)

        except requests_exceptions_safe() as e:
            print(f"       Erreur reseau TikTok : {e}")
            break
        except Exception as e:
            print(f"       Erreur TikTok : {e}")
            break

    return [tiktok_comment_to_comment_like(c) for c in all_comments]


def requests_exceptions_safe():
    import requests
    return requests.RequestException


# ---------------------------------------------------------------------------
# Dispatch par plateforme
# ---------------------------------------------------------------------------

def fetch_comments_for_media_post(plateforme, url):
    plateforme_norm = (plateforme or "").strip().lower()
    if plateforme_norm in ("x", "twitter"):
        tweet_id = extract_tweet_id(url)
        if not tweet_id:
            print(f"       Impossible d'extraire l'ID du tweet depuis : {url}")
            return []
        replies = get_tweet_replies(tweet_id)
        return [tweet_to_comment_like(r) for r in replies]
    if plateforme_norm == "tiktok":
        return get_all_tiktok_comments(url)
    print(f" Plateforme non geree : {plateforme}")
    return []


# ---------------------------------------------------------------------------
# Traitement d'une entree urls_du_jour.json
# ---------------------------------------------------------------------------

def process_entry(entry, pending, corpus):
    url = entry.get("url")
    media = entry.get("media", "Media inconnu")
    plateforme = entry.get("plateforme", "")
    pays = entry.get("pays", [])  # liste : un post peut concerner plusieurs pays
    titre = entry.get("titre", "")
    published_at = entry.get("published_at")

    if not url:
        print(" Entree sans URL, ignoree.")
        return

    print()
    print(f"    Post : {media} ({plateforme}) · pays={pays}")
    print(f"      URL : {url}")

    print("       Recuperation des commentaires (1ere capture)")
    comments = fetch_comments_for_media_post(plateforme, url)
    print(f"       {len(comments)} commentaire(s) recupere(s)")

    plateforme_norm = (plateforme or "").strip().lower()
    detail_key = "tweet_details" if plateforme_norm in ("x", "twitter") else "tiktok_details"

    _append_or_merge_target(corpus, target_name=media, platform=plateforme_norm,
        target_url=url, country_code=pays, detail_key=detail_key,
        post_detail={"url": url, "title": titre, "published_at": published_at},
        comments=comments, recheck_status="initial")

    item = {
        "url": url,
        "media": media,
        "plateforme": plateforme,
        "pays": pays,
        "titre": titre,
        "published_at": published_at,
        "added_at": now_utc().isoformat(),
    }
    if not any(pending_key(p) == url for p in pending):
        pending.append(item)
        print(f"       Reverification programmee dans ~{RECHECK_DELAY_HOURS}h")


def process_pending(pending, corpus):
    if not pending:
        print(" Aucun post media a reverifier.")
        return

    print()
    print("====================================================")
    print(" REVERIFICATION DES POSTS MEDIAS PROGRAMMES")
    print("====================================================")

    for item in list(pending):
        url = item.get("url")
        added_at = parse_datetime(item.get("added_at")) or now_utc()
        age = now_utc() - added_at
        age_hours = age.total_seconds() / 3600
        delai_ecoule = age >= timedelta(hours=RECHECK_DELAY_HOURS)

        print()
        print(f" En file : {url}")
        print(f"    Capture il y a {age_hours:.1f}h "
              f"({'delai atteint' if delai_ecoule else f'< {RECHECK_DELAY_HOURS}h, on patiente'})")

        if not delai_ecoule:
            continue

        print(f"    Reverification finale ({item.get('plateforme')})")
        comments = fetch_comments_for_media_post(item.get("plateforme"), url)
        print(f"    Commentaires au final : {len(comments)}")

        plateforme_norm = (item.get("plateforme") or "").strip().lower()
        detail_key = "tweet_details" if plateforme_norm in ("x", "twitter") else "tiktok_details"

        _append_or_merge_target(corpus, target_name=item.get("media"), platform=plateforme_norm,
            target_url=url, country_code=item.get("pays", []), detail_key=detail_key,
            post_detail={"url": url, "title": item.get("titre"), "published_at": item.get("published_at")},
            comments=comments, recheck_status="final")

        pending[:] = [p for p in pending if pending_key(p) != url]
        print("    Reverifie une fois -> retire de la file definitivement")
        time.sleep(SLEEP_BETWEEN_REQUESTS)


def main():
    print()
    print("====================================================")
    print(" COLLECTE MEDIAS INTERNATIONAUX (X + TikTok)")
    print("====================================================")

    entries = load_urls_du_jour(URLS_DU_JOUR_PATH)
    print(f" {len(entries)} URL(s) trouvee(s) dans {URLS_DU_JOUR_PATH}")

    pending = load_pending()
    print(f"⏳ {len(pending)} post(s) deja en attente de reverification.")

    corpus = []

    process_pending(pending, corpus)
    save_pending(pending)
    save_corpus(corpus)

    already_seen_urls = {p.get("url") for p in pending} | {c.get("url") for c in corpus}

    for entry in entries:
        url = entry.get("url")
        if url in already_seen_urls:
            print(f" Deja collecte ou en file, on passe : {url}")
            continue
        process_entry(entry, pending, corpus)
        save_pending(pending)
        save_corpus(corpus)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    save_pending(pending)
    save_corpus(corpus)

    total_comments = sum(
        p.get("comments_count", 0) for c in corpus for p in c.get("posts_collectes", [])
    )

    print()
    print("====================================================")
    print(" COLLECTE MEDIAS INTERNATIONAUX TERMINEE")
    print("====================================================")
    print(f" Posts traites ce run : {len(corpus)}")
    print(f" Commentaires collectes ce run : {total_comments}")
    print(f" Posts en attente de reverification (~{RECHECK_DELAY_HOURS}h) : {len(pending)}")
    print(f" Sortie : {OUTPUT_JSON}")
    print("====================================================")


if __name__ == "__main__":
    main()