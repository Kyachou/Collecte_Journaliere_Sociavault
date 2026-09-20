import os
import re
import json
import time
from datetime import datetime, timezone

from collecte import (
    api_get,
    BASE_URL,
    DATA_DIR,
    get_tweet_replies,
    tweet_to_comment_like,
    now_utc,
    SLEEP_BETWEEN_REQUESTS,
    PENDING_LOW_ENGAGEMENT_THRESHOLD,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Chemin du fichier depose par l'agent de curation (synchronise depuis GitHub).
# Adapter si le fichier est syncrhonise ailleurs que a cote de ce script.
URLS_DU_JOUR_PATH = os.environ.get(
    "URLS_DU_JOUR_PATH", os.path.join(DATA_DIR, "urls_du_jour.json")
)

PENDING_MEDIAS_FILE = os.path.join(DATA_DIR, "pending_medias_internationaux.json")
OUTPUT_JSON = os.path.join(
    DATA_DIR,
    f"sociavault_medias_internationaux_raw{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json",
)

# --- NOUVEAU : plafonds anti-boucle-infinie sur la pagination TikTok ---
MAX_TIKTOK_COMMENT_PAGES = 20
MAX_TIKTOK_REPLY_PAGES = 5

# --- NOUVEAU : seuil pour aller directement au raw JSON sans passer par le pending ---
INITIAL_DIRECT_THRESHOLD = 10

# --- NOUVEAU : watchdog de durée globale ---
# IMPORTANT : doit rester INFÉRIEUR au "timeout-minutes" du step correspondant
# dans cron_collecte.yml (80 min pour "Lancer la collecte internationale").
RUN_TIME_BUDGET_HOURS = 1.0  # 60 min, marge de 20 min sous le timeout YAML
MAX_RUNTIME_SECONDS = RUN_TIME_BUDGET_HOURS * 3600
SCRIPT_START = time.time()


def runtime_budget_exceeded():
    return (time.time() - SCRIPT_START) > MAX_RUNTIME_SECONDS


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

# Seuil au-dela duquel on paie l'appel "Comment Replies" (1 credit/appel).
# Sous ce seuil, on laisse les reponses de ce commentaire de cote plutot que
# de depenser un credit pour 1 ou 2 reponses. A ajuster selon le budget.
TIKTOK_REPLIES_MIN_COUNT = 3


def get_tiktok_comment_replies(video_url, comment_id):
    """
    Recupere les reponses a UN commentaire TikTok donne.

    Endpoint : GET /v1/scrape/tiktok/comment-replies?comment_id=...&url=...
    (1 credit par appel, cf. catalogue SociaVault "Comment Replies"). Chemin
    confirme via l'URL du playground officiel SociaVault (tiret, ni underscore
    ni slash comme les tentatives precedentes).

    Forme de reponse CONFIRMEE par un test reel (endpoint: "tiktok/comment_replies"
    dans la reponse) : meme enveloppe maison que /scrape/tiktok/comments
    (data.comments dict indexe par cle numerique-string, has_more, cursor).

    Retourne None (au lieu de []) si le tout premier appel echoue (timeout,
    erreur reseau, HTTP non-200) : signale "resultat non fiable, a reessayer"
    plutot que de laisser croire a un vrai zero confirme. Si l'echec survient
    apres au moins une page reussie, on garde ce qui a deja ete recupere.

    NOUVEAU : plafonnee a MAX_TIKTOK_REPLY_PAGES pages pour eviter qu'un
    commentaire a tres nombreuses reponses ne bloque le script indefiniment.
    """
    endpoint = f"{BASE_URL}/scrape/tiktok/comment-replies"
    all_replies = []
    seen_ids = set()
    cursor = 0
    page = 1

    while page <= MAX_TIKTOK_REPLY_PAGES:
        params = {"comment_id": comment_id, "url": video_url, "cursor": cursor}
        try:
            res = api_get(endpoint, params=params, timeout=30)
            if res is None:
                if page == 1:
                    print("          Echec reseau des la 1ere page -- resultat non fiable")
                    return None
                break
            if res.status_code != 200:
                print(f"          Erreur TikTok replies HTTP {res.status_code} : {res.text[:300]}")
                if page == 1:
                    return None
                break

            response = res.json()

            data = response.get("data", {}) if isinstance(response.get("data"), dict) else response
            replies_data = data.get("comments", data.get("replies", []))
            has_more = bool(data.get("has_more", False))
            next_cursor = data.get("cursor")

            if isinstance(replies_data, dict):
                batch = list(replies_data.values())
            elif isinstance(replies_data, list):
                batch = replies_data
            else:
                batch = []

            for r in batch:
                if not isinstance(r, dict):
                    continue
                rid = r.get("cid") or r.get("comment_id") or r.get("id")
                if rid and rid in seen_ids:
                    continue
                if rid:
                    seen_ids.add(rid)
                all_replies.append(r)

            if not has_more or next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor
            page += 1
            time.sleep(SLEEP_BETWEEN_REQUESTS)

        except requests_exceptions_safe() as e:
            print(f"          Erreur reseau TikTok replies : {e}")
            if page == 1:
                return None
            break
        except Exception as e:
            print(f"          Erreur TikTok replies : {e}")
            if page == 1:
                return None
            break

    else:
        print(f"          Plafond de {MAX_TIKTOK_REPLY_PAGES} pages de reponses TikTok atteint — arrêt.")

    return all_replies


def tiktok_comment_to_comment_like(c):
    if not isinstance(c, dict):
        # Certains elements de reply_comment/replies ne sont pas des objets
        # complets (ex. simple chaine) selon les posts. On ignore proprement
        # plutot que de planter tout le run pour un post.
        return {
            "id": None, "text": None, "created_at": None,
            "reply_count": 0, "like_count": 0,
            "author": {}, "replies": [],
        }
    user = c.get("user") or {}
    created = c.get("create_time")
    created_iso = None
    if isinstance(created, (int, float)):
        try:
            created_iso = datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
        except Exception:
            created_iso = None
    raw_replies = c.get("reply_comment") or c.get("replies") or []
    if isinstance(raw_replies, dict):
        # Meme convention que le champ "comments" racine : dict indexe par
        # cle numerique-string ("0","1",...), pas une liste.
        raw_replies = list(raw_replies.values())
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
        "replies": [
            tiktok_comment_to_comment_like(r) for r in raw_replies if isinstance(r, dict)
        ],
    }


def _dump_debug_once(response_json):
    """
    No-op conserve pour compatibilite avec les appels existants (get_all_tiktok_comments
    l'appelle encore). Les formats de reponse TikTok (/comments et
    /comment-replies) ont ete confirmes par des tests reels, plus besoin
    d'ecrire de fichier de debug a chaque run.
    """
    return


def get_all_tiktok_comments(video_url):
    """
    Recupere tous les commentaires d'une video TikTok.

    Endpoint : GET /v1/scrape/tiktok/comments?url=...&cursor=...

    Format de pagination CONFIRME via test reel : {"has_more": 0|1, "cursor":
    <entier>, "total": N}, PAS has_next_page/cursor comme sur Facebook.

    reply_comment est souvent `null` ou incomplet sur les commentaires racine
    meme quand reply_comment_total > 0 : les reponses completes sont
    recuperees separement via get_tiktok_comment_replies (endpoint
    /scrape/tiktok/comment-replies) au-dela du seuil TIKTOK_REPLIES_MIN_COUNT,
    voir plus bas dans cette fonction.

    Retourne None (au lieu de []) si le tout premier appel de la page
    racine echoue : signale "pas verifie, a reessayer" plutot qu'un faux
    zero confirme.

    NOUVEAU : plafonnee a MAX_TIKTOK_COMMENT_PAGES pages pour eviter qu'une
    video tres commentee ne bloque le script indefiniment.
    """
    endpoint = f"{BASE_URL}/scrape/tiktok/comments"
    all_comments = []
    seen_ids = set()
    cursor = 0
    page = 1
    first_response_logged = False

    while page <= MAX_TIKTOK_COMMENT_PAGES:
        params = {"url": video_url, "cursor": cursor}
        try:
            print(f"       TikTok commentaires page {page}")
            res = api_get(endpoint, params=params, timeout=30)
            if res is None:
                if page == 1:
                    print("       Echec reseau des la 1ere page -- resultat non fiable")
                    return None
                break
            print(f"         ↳ HTTP {res.status_code}")
            if res.status_code != 200:
                print(f"       Erreur TikTok : {res.text[:500]}")
                if page == 1:
                    return None
                break

            response = res.json()
            if not first_response_logged:
                _dump_debug_once(response)
                first_response_logged = True

            data = response.get("data", {}) if isinstance(response.get("data"), dict) else response
            comments_data = data.get("comments", [])
            has_more = bool(data.get("has_more", False))
            next_cursor = data.get("cursor")

            if isinstance(comments_data, dict):
                comments_batch = list(comments_data.values())
            elif isinstance(comments_data, list):
                comments_batch = comments_data
            else:
                comments_batch = []

            print(f"          {len(comments_batch)} commentaire(s) (total annonce : {data.get('total')})")
            for c in comments_batch:
                if not isinstance(c, dict):
                    continue
                cid = c.get("cid") or c.get("comment_id") or c.get("id")
                if cid and cid in seen_ids:
                    continue
                if cid:
                    seen_ids.add(cid)
                all_comments.append(c)

            if not has_more or next_cursor is None or next_cursor == cursor:
                print("          Fin des commentaires TikTok")
                break
            cursor = next_cursor
            page += 1
            time.sleep(SLEEP_BETWEEN_REQUESTS)

        except requests_exceptions_safe() as e:
            print(f"       Erreur reseau TikTok : {e}")
            if page == 1:
                return None
            break
        except Exception as e:
            print(f"       Erreur TikTok : {e}")
            if page == 1:
                return None
            break

    else:
        print(f"       Plafond de {MAX_TIKTOK_COMMENT_PAGES} pages de commentaires TikTok atteint — arrêt.")

    print(f"       {len(all_comments)} commentaire(s) racine au total pour cette video")

    for c in all_comments:
        if not isinstance(c, dict):
            continue
        reply_total = c.get("reply_comment_total", 0) or 0
        if reply_total < TIKTOK_REPLIES_MIN_COUNT:
            continue
        cid = c.get("cid") or c.get("comment_id") or c.get("id")
        if not cid:
            continue
        print(f"       {reply_total} reponse(s) annoncee(s) pour le commentaire {cid}")
        replies = get_tiktok_comment_replies(video_url, cid)
        if replies is None:
            print("          Echec reseau sur cette recuperation -- reply_comment d'origine conserve")
        else:
            print(f"          {len(replies)} reponse(s) recuperee(s)")
            c["reply_comment"] = replies
        time.sleep(SLEEP_BETWEEN_REQUESTS)

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
        if replies is None:
            return None
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
    if comments is None:
        comments = []
    count = len(comments)
    print(f"       {count} commentaire(s) recupere(s)")

    plateforme_norm = (plateforme or "").strip().lower()
    detail_key = "tweet_details" if plateforme_norm in ("x", "twitter") else "tiktok_details"

    if count > INITIAL_DIRECT_THRESHOLD:
        # Assez de commentaires des la capture : direct dans le raw JSON, pas de pending.
        _append_or_merge_target(corpus, target_name=media, platform=plateforme_norm,
            target_url=url, country_code=pays, detail_key=detail_key,
            post_detail={"url": url, "title": titre, "published_at": published_at},
            comments=comments, recheck_status="initial_direct")
        print(f"       >{INITIAL_DIRECT_THRESHOLD} commentaires -> ajoute directement au raw JSON")
        return

    # Pas assez de commentaires : en attente d'une verification UNIQUE au run suivant.
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
        print(f"       ≤{INITIAL_DIRECT_THRESHOLD} commentaires -> mis en pending "
              f"(verification unique au prochain run)")


def process_pending(pending, corpus):
    """
    Verification UNIQUE des posts medias en pending, au run suivant leur capture :
    - Chaque post est verifie une seule fois, puis TOUJOURS retire du pending,
      que le resultat soit positif ou negatif :
        - > PENDING_LOW_ENGAGEMENT_THRESHOLD commentaires -> garde, ajoute au
          raw JSON (status "recheck_ok").
        - <= PENDING_LOW_ENGAGEMENT_THRESHOLD commentaires -> jete, PAS ajoute
          au raw JSON.
    - Seul un echec reseau laisse le post en pending pour retenter au run suivant
      (ne compte pas comme la verification).
    - Sauvegarde incrementale apres chaque item traite.
    """
    if not pending:
        print(" Aucun post media a reverifier.")
        return

    print()
    print("====================================================")
    print(" VERIFICATION UNIQUE DES POSTS MEDIAS EN PENDING")
    print("====================================================")

    for item in list(pending):
        if runtime_budget_exceeded():
            print(" Budget de temps global atteint pendant le pending — arret propre.")
            save_pending(pending)
            save_corpus(corpus)
            return

        url = item.get("url")

        print()
        print(f" En file : {url}")
        print(f"    Verification unique ({item.get('plateforme')})")
        comments = fetch_comments_for_media_post(item.get("plateforme"), url)

        if comments is None:
            print("    Echec reseau au recheck -- on reessaiera au prochain run, "
                  "pas retire du pending (ne compte pas comme la verification).")
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            continue

        current_count = len(comments)
        print(f"    Commentaires actuels : {current_count}")

        plateforme_norm = (item.get("plateforme") or "").strip().lower()
        detail_key = "tweet_details" if plateforme_norm in ("x", "twitter") else "tiktok_details"

        # Toujours retire du pending apres cette verification, garde ou jete.
        pending[:] = [p for p in pending if pending_key(p) != url]

        if current_count > PENDING_LOW_ENGAGEMENT_THRESHOLD:
            _append_or_merge_target(corpus, target_name=item.get("media"), platform=plateforme_norm,
                target_url=url, country_code=item.get("pays", []), detail_key=detail_key,
                post_detail={"url": url, "title": item.get("titre"), "published_at": item.get("published_at")},
                comments=comments, recheck_status="recheck_ok")
            print(f"    >{PENDING_LOW_ENGAGEMENT_THRESHOLD} commentaires -> garde, ajoute au raw JSON")
        else:
            print(f"    <={PENDING_LOW_ENGAGEMENT_THRESHOLD} commentaires -> jete (pas dans le raw JSON)")

        save_pending(pending)
        save_corpus(corpus)
        time.sleep(SLEEP_BETWEEN_REQUESTS)


def main():
    print()
    print("====================================================")
    print(" COLLECTE MEDIAS INTERNATIONAUX (X + TikTok)")
    print("====================================================")
    print(f"Budget de temps global : {MAX_RUNTIME_SECONDS / 3600:.1f}h")
    print(f">{INITIAL_DIRECT_THRESHOLD} commentaires à la capture → direct au raw JSON")
    print(f"≤{INITIAL_DIRECT_THRESHOLD} commentaires à la capture → pending, "
          f"vérifié une seule fois au run suivant "
          f"(gardé si >{PENDING_LOW_ENGAGEMENT_THRESHOLD}, jeté sinon)")

    entries = load_urls_du_jour(URLS_DU_JOUR_PATH)
    print(f" {len(entries)} URL(s) trouvee(s) dans {URLS_DU_JOUR_PATH}")

    pending = load_pending()
    print(f"⏳ {len(pending)} post(s) deja en attente de reverification.")

    corpus = []

    process_pending(pending, corpus)
    save_pending(pending)
    save_corpus(corpus)

    already_seen_urls = {p.get("url") for p in pending} | {c.get("target_url") for c in corpus}

    if runtime_budget_exceeded():
        print(" Budget de temps atteint apres le pending — collecte de nouvelles URLs sautee.")
    else:
        for entry in entries:
            if runtime_budget_exceeded():
                print(" Budget de temps global atteint — arret propre avant la fin des URLs.")
                break

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
    print(f" Posts en attente de reverification : {len(pending)}")
    print(f" Sortie : {OUTPUT_JSON}")
    print("====================================================")


if __name__ == "__main__":
    main()