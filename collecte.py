import os
import time
import json
import yaml
import requests
from datetime import datetime, timezone, timedelta


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

YAML_PATH = os.path.join(BASE_DIR, "targets.yaml")
DATA_DIR = os.path.join(BASE_DIR, "data")

PENDING_FILE = os.path.join(DATA_DIR, "pending_posts.json")

OUTPUT_JSON = os.path.join(
    DATA_DIR,
    f"sociavault_raw{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
)

SOCIAVAULT_API_KEY = os.environ.get("SOCIAVAULT_API_KEY")

if not SOCIAVAULT_API_KEY:
    raise RuntimeError(
        "❌ La variable d'environnement SOCIAVAULT_API_KEY n'est pas définie."
    )


BASE_URL = "https://api.sociavault.com/v1"

HEADERS = {
    "X-API-Key": SOCIAVAULT_API_KEY,
    "Content-Type": "application/json"
}


# ============================================================
# PARAMÈTRES DU WORKFLOW
# ============================================================

WINDOW_HOURS = 24

SEUIL_COMMENTAIRES = 10

# --- Règles du pending ---
# Durée max qu'un item peut rester en pending avant décision finale.
PENDING_MAX_DAYS = 3
# Si le délai de PENDING_MAX_DAYS est écoulé, on accepte quand même
# l'item s'il a atteint ce seuil réduit (au lieu de SEUIL_COMMENTAIRES).
SEUIL_COMMENTAIRES_APRES_DELAI = 5

# Facebook
MAX_FACEBOOK_PAGES = 10
MAX_OLD_CONSECUTIVE = 3

# Réponses aux commentaires Facebook (thread niveau 1 uniquement)
# On ne va chercher les réponses que si reply_count >= ce seuil,
# pour limiter la consommation de crédits sur les commentaires très populaires.
REPLIES_MIN_COUNT = 7

# Twitter / X
MAX_TWITTER_TWEET_PAGES = 3       # pagination de la liste de tweets d'un profil
MAX_TWITTER_REPLIES_PAGES = 5     # pagination des réponses à un tweet donné

REQUEST_TIMEOUT = 30
SLEEP_BETWEEN_REQUESTS = 1


# ============================================================
# UTILITAIRES DATE / GÉNÉRIQUES
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def cutoff_24h():
    return now_utc() - timedelta(hours=WINDOW_HOURS)


def parse_datetime(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except Exception:
            return None

    if not isinstance(value, str):
        return None

    value = value.strip()
    if not value:
        return None

    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass

    return None


def get_post_date(post):
    """
    Date générique d'un item du corpus (post Facebook OU tweet normalisé).
    Utilise 'creation_time' en priorité (les deux plateformes y sont normalisées),
    puis 'publishTime' en repli.
    """
    creation_time = post.get("creation_time")
    if creation_time:
        dt = parse_datetime(creation_time)
        if dt:
            return dt

    publish_time = post.get("publishTime")
    if publish_time:
        dt = parse_datetime(publish_time)
        if dt:
            return dt

    return None


def get_post_id(post):
    return str(post.get("id") or "").strip()


def get_post_url(post):
    return (
        post.get("url")
        or post.get("permalink")
        or post.get("post_url")
    )


# ============================================================
# FICHIERS
# ============================================================

def load_targets(yaml_file):
    if not os.path.exists(yaml_file):
        return []
    with open(yaml_file, "r", encoding="utf-8") as f:
        content = yaml.safe_load(f) or {}
    return content.get("targets", [])


def load_pending():
    if not os.path.exists(PENDING_FILE):
        return []
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"⚠️ Impossible de lire pending_posts.json : {e}")
        return []


def save_pending(pending):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)


def save_corpus(corpus):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)


# ============================================================
# PENDING (générique, indépendant de la plateforme)
# ============================================================

def pending_key(item):
    post = item.get("post", item)
    post_id = get_post_id(post)
    if post_id:
        return post_id
    url = item.get("post_url") or post.get("url") or post.get("permalink")
    return url


def add_to_pending(pending, post, target_name, platform, target_url):
    post_id = get_post_id(post)
    post["_target_url"] = target_url

    item = {
        "post": post,
        "post_id": post_id,
        "post_url": get_post_url(post),
        "target_name": target_name,
        "target_url": target_url,
        "platform": platform,
        "added_at": now_utc().isoformat()
    }

    new_key = pending_key(item)
    for existing in pending:
        if pending_key(existing) == new_key:
            return False

    pending.append(item)
    print(f"      ⏳ Ajout pending : {post_id or get_post_url(post)}")
    return True


def remove_from_pending(pending, key):
    new_pending = [item for item in pending if pending_key(item) != key]
    removed = len(new_pending) != len(pending)
    pending[:] = new_pending
    return removed


# ============================================================
# FACEBOOK : POSTS
# ============================================================

def extract_posts_from_response(response):
    data = response.get("data", {})
    posts_data = data.get("posts", {})
    if isinstance(posts_data, dict):
        return list(posts_data.values())
    if isinstance(posts_data, list):
        return posts_data
    return []


def get_facebook_recent_posts(page_url):
    endpoint = f"{BASE_URL}/scrape/facebook/profile/posts"
    recent_posts = []
    cursor = None
    page_num = 1
    old_consecutive = 0
    seen_ids = set()
    cutoff = cutoff_24h()

    print(f"   🕐 Recherche des posts depuis {cutoff.isoformat()}")

    while page_num <= MAX_FACEBOOK_PAGES:
        params = {"url": page_url}
        if cursor:
            params["cursor"] = cursor

        try:
            print(f"   ⌛ Scraping Facebook (page {page_num})...")
            res = requests.get(endpoint, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
            print(f"   ↳ HTTP {res.status_code}")

            if res.status_code != 200:
                print(f"   ❌ Erreur Facebook : {res.text[:500]}")
                break

            response = res.json()
            posts_batch = extract_posts_from_response(response)
            print(f"   📦 {len(posts_batch)} post(s) reçus.")

            if not posts_batch:
                print("   🛑 Aucun post reçu.")
                break

            page_recent = 0
            page_old = 0

            for post in posts_batch:
                post_id = get_post_id(post)
                if post_id and post_id in seen_ids:
                    continue
                if post_id:
                    seen_ids.add(post_id)

                post_date = get_post_date(post)
                if not post_date:
                    print(f"      ⚠️ Date inconnue : {post_id}")
                    continue

                if post_date >= cutoff:
                    recent_posts.append(post)
                    page_recent += 1
                    old_consecutive = 0
                    print(f"      ✅ Récent : {post_date.isoformat()}")
                else:
                    page_old += 1
                    old_consecutive += 1
                    print(f"      ⏭️ Ancien : {post_date.isoformat()} | ancien consécutif={old_consecutive}")

            print(f"   📊 Page {page_num} : {page_recent} récent(s), {page_old} ancien(s)")

            data = response.get("data", {})
            cursor = data.get("cursor") or data.get("next_cursor")

            if not cursor:
                print("   🛑 Aucun cursor suivant.")
                break

            if old_consecutive >= MAX_OLD_CONSECUTIVE:
                print(f"   🛑 {MAX_OLD_CONSECUTIVE} posts anciens consécutifs rencontrés.")
                print("   🛑 Arrêt de la pagination Facebook.")
                break

            page_num += 1
            time.sleep(SLEEP_BETWEEN_REQUESTS)

        except requests.RequestException as e:
            print(f"   ❌ Erreur réseau Facebook : {e}")
            break
        except Exception as e:
            print(f"   ❌ Erreur Facebook : {e}")
            break

    recent_posts.sort(
        key=lambda p: get_post_date(p) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True
    )
    return recent_posts


def get_all_facebook_comments(post_url):
    endpoint = f"{BASE_URL}/scrape/facebook/post/comments"
    all_comments = []
    seen_ids = set()
    cursor = None
    page = 1

    while True:
        params = {"url": post_url}
        if cursor:
            params["cursor"] = cursor

        try:
            print(f"      💬 Commentaires page {page}")
            res = requests.get(endpoint, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
            print(f"         ↳ HTTP {res.status_code}")

            if res.status_code != 200:
                print(f"      ❌ Erreur Sociavault : {res.text[:500]}")
                break

            response = res.json()
            if not response.get("success"):
                print("      ❌ Sociavault success=false")
                break

            data = response.get("data", {})
            comments_data = data.get("comments", {})

            if isinstance(comments_data, dict):
                comments_batch = list(comments_data.values())
            elif isinstance(comments_data, list):
                comments_batch = comments_data
            else:
                comments_batch = []

            print(f"         📥 {len(comments_batch)} commentaire(s)")

            for c in comments_batch:
                cid = c.get("id")
                if cid and cid in seen_ids:
                    continue
                if cid:
                    seen_ids.add(cid)
                all_comments.append(c)

            has_next_page = data.get("has_next_page", False)
            next_cursor = data.get("cursor")

            if not has_next_page:
                print("         ✅ Fin des commentaires")
                break
            if not next_cursor:
                print("         ⚠️ Page suivante annoncée mais cursor absent")
                break

            cursor = next_cursor
            page += 1
            time.sleep(SLEEP_BETWEEN_REQUESTS)

        except requests.RequestException as e:
            print(f"      ❌ Erreur réseau commentaires : {e}")
            break
        except Exception as e:
            print(f"      ❌ Erreur commentaires : {e}")
            break

    return enrich_comments_with_replies(all_comments)


def get_comment_replies(feedback_id, expansion_token):
    """
    Récupère les réponses (niveau 1) à un commentaire Facebook via
    /scrape/facebook/comment/replies (feedback_id + expansion_token).
    """
    endpoint = f"{BASE_URL}/scrape/facebook/comment/replies"
    all_replies = []
    seen_ids = set()
    cursor = None
    page = 1

    while True:
        params = {
            "feedback_id": feedback_id,
            "expansion_token": expansion_token
        }
        if cursor:
            params["cursor"] = cursor

        try:
            res = requests.get(endpoint, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
            if res.status_code != 200:
                print(f"         ⚠️ Erreur replies HTTP {res.status_code} : {res.text[:200]}")
                break

            response = res.json()
            if not response.get("success"):
                print("         ❌ Sociavault success=false (replies)")
                break

            data = response.get("data", {})
            replies_data = data.get("replies", {})

            if isinstance(replies_data, dict):
                batch = list(replies_data.values())
            elif isinstance(replies_data, list):
                batch = replies_data
            else:
                batch = []

            if not batch:
                break

            for r in batch:
                rid = r.get("id")
                if rid and rid in seen_ids:
                    continue
                if rid:
                    seen_ids.add(rid)
                all_replies.append(r)

            has_next_page = data.get("has_next_page", False)
            next_cursor = data.get("cursor")

            if not has_next_page or not next_cursor:
                break

            cursor = next_cursor
            page += 1
            time.sleep(SLEEP_BETWEEN_REQUESTS)

        except Exception as e:
            print(f"         ⚠️ Erreur replies : {e}")
            break

    return all_replies


def enrich_comments_with_replies(comments):
    """
    Pour chaque commentaire avec reply_count >= REPLIES_MIN_COUNT,
    va chercher ses réponses et les attache sous comment['replies'].
    Les commentaires sans assez de réponses reçoivent une liste vide.
    """
    for c in comments:
        reply_count = c.get("reply_count", 0) or 0

        if reply_count < REPLIES_MIN_COUNT:
            c["replies"] = []
            continue

        feedback_id = c.get("feedback_id")
        expansion_token = c.get("expansion_token")

        if not feedback_id or not expansion_token:
            c["replies"] = []
            continue

        print(f"      🔽 {reply_count} réponse(s) annoncée(s) pour le commentaire {c.get('id')}")
        replies = get_comment_replies(feedback_id, expansion_token)
        print(f"         📥 {len(replies)} réponse(s) récupérée(s)")
        c["replies"] = replies
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    return comments


# ============================================================
# TWITTER / X : PROFIL, TWEETS, RÉPONSES
# ============================================================

def get_twitter_user_rest_id(handle):
    endpoint = f"{BASE_URL}/scrape/twitter/profile"
    try:
        res = requests.get(endpoint, headers=HEADERS, params={"handle": handle}, timeout=20)
        if res.status_code != 200:
            print(f"⚠️ Erreur profil Twitter HTTP {res.status_code}")
            return None
        data = res.json().get("data", {})
        return data.get("rest_id") or data.get("id")
    except Exception as e:
        print(f"⚠️ Erreur Twitter profil : {e}")
        return None


def extract_tweets_from_timeline(data):
    """
    Parcourt data.result.timeline.instructions (structure commune à
    user-tweets-all ET à search) pour en extraire les tweets.
    """
    tweets = []
    instructions = data.get("result", {}).get("timeline", {}).get("instructions", [])

    for instr in instructions:
        instr_type = instr.get("type")

        if instr_type == "TimelinePinEntry":
            entry = instr.get("entry", {})
            tweet_result = (
                entry.get("content", {})
                .get("itemContent", {})
                .get("tweet_results", {})
                .get("result")
            )
            if tweet_result:
                tweets.append(tweet_result)

        elif instr_type == "TimelineAddEntries":
            for entry in instr.get("entries", []):
                tweet_result = (
                    entry.get("content", {})
                    .get("itemContent", {})
                    .get("tweet_results", {})
                    .get("result")
                )
                if tweet_result:
                    tweets.append(tweet_result)

    return tweets


def get_twitter_recent_tweets(profile_url):
    handle = profile_url.rstrip("/").split("/")[-1]

    rest_id = get_twitter_user_rest_id(handle)
    if not rest_id:
        return []

    endpoint = f"{BASE_URL}/scrape/twitter/user-tweets-all"
    all_tweets = []
    cursor = None
    page = 1

    while page <= MAX_TWITTER_TWEET_PAGES:
        params = {"user_id": rest_id}
        if cursor:
            params["cursor"] = cursor

        try:
            res = requests.get(endpoint, headers=HEADERS, params=params, timeout=20)
            if res.status_code != 200:
                print(f"⚠️ Erreur tweets HTTP {res.status_code}")
                break

            data = res.json().get("data", {})
            batch = extract_tweets_from_timeline(data)
            if not batch:
                break

            all_tweets.extend(batch)

            cursor_data = data.get("cursor", {})
            cursor = cursor_data.get("bottom") if isinstance(cursor_data, dict) else None
            if not cursor:
                break

            page += 1
            time.sleep(SLEEP_BETWEEN_REQUESTS)

        except Exception as e:
            print(f"⚠️ Erreur Twitter : {e}")
            break

    return all_tweets


def get_tweet_replies(tweet_id):
    """
    Récupère les réponses (commentaires) à un tweet via
    /scrape/twitter/search?query=conversation_id:{tweet_id}

    Exclut le tweet racine (le post original) du résultat.
    """
    endpoint = f"{BASE_URL}/scrape/twitter/search"
    all_replies = []
    seen_ids = set()
    cursor = None
    page = 1

    while page <= MAX_TWITTER_REPLIES_PAGES:
        params = {"query": f"conversation_id:{tweet_id}"}
        if cursor:
            params["cursor"] = cursor

        try:
            print(f"      💬 Réponses Twitter page {page}")
            res = requests.get(endpoint, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
            print(f"         ↳ HTTP {res.status_code}")

            if res.status_code != 200:
                print(f"      ⚠️ Erreur replies Twitter HTTP {res.status_code}")
                break

            payload = res.json()
            data = payload.get("data", {})

            batch = extract_tweets_from_timeline(data)
            if not batch:
                print("         ✅ Fin des réponses")
                break

            for tweet in batch:
                legacy = tweet.get("legacy", {}) or {}
                rest_id = tweet.get("rest_id") or legacy.get("id_str")

                # Exclure le tweet racine (le post original)
                if rest_id == tweet_id:
                    continue
                if rest_id and rest_id in seen_ids:
                    continue
                if rest_id:
                    seen_ids.add(rest_id)

                all_replies.append(tweet)

            print(f"         📥 {len(all_replies)} réponse(s) cumulée(s)")

            cursor_data = data.get("cursor", {})
            cursor = cursor_data.get("bottom") if isinstance(cursor_data, dict) else None
            if not cursor:
                break

            page += 1
            time.sleep(SLEEP_BETWEEN_REQUESTS)

        except Exception as e:
            print(f"      ⚠️ Erreur replies Twitter : {e}")
            break

    return all_replies


def tweet_to_comment_like(tweet):
    """
    Convertit un tweet-réponse en objet 'comment-like', cohérent
    avec le format utilisé côté Facebook.
    """
    legacy = tweet.get("legacy", {}) or {}
    user = (
        tweet.get("core", {})
        .get("user_results", {})
        .get("result", {})
        .get("legacy", {})
    ) or {}

    return {
        "id": tweet.get("rest_id") or legacy.get("id_str"),
        "text": legacy.get("full_text"),
        "created_at": legacy.get("created_at"),
        "reply_count": legacy.get("reply_count", 0),
        "favorite_count": legacy.get("favorite_count", 0),
        "retweet_count": legacy.get("retweet_count", 0),
        "author": {
            "name": user.get("name"),
            "screen_name": user.get("screen_name"),
        },
    }


def normalize_tweet(tweet):
    """
    Ajoute au dict du tweet les champs génériques (id, url, creation_time,
    commentCount) attendus par les fonctions communes (pending, dates, seuil).
    Modifie le tweet en place ET le retourne.
    """
    legacy = tweet.get("legacy", {}) or {}
    user = (
        tweet.get("core", {})
        .get("user_results", {})
        .get("result", {})
        .get("legacy", {})
    ) or {}

    rest_id = tweet.get("rest_id") or legacy.get("id_str")
    screen_name = user.get("screen_name")

    tweet["id"] = rest_id
    tweet["url"] = (
        f"https://x.com/{screen_name}/status/{rest_id}"
        if screen_name and rest_id else None
    )
    tweet["commentCount"] = legacy.get("reply_count", 0)

    created_at = legacy.get("created_at")
    if created_at:
        try:
            dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc)
            tweet["creation_time"] = dt.isoformat()
        except Exception:
            tweet["creation_time"] = None
    else:
        tweet["creation_time"] = None

    return tweet


# ============================================================
# TRAITEMENT GÉNÉRIQUE D'UN ITEM (post FB ou tweet normalisé)
# AVEC SEUIL DE COMMENTAIRES
# ============================================================

def fetch_comments_for(platform, post):
    """
    Point d'entrée unique pour récupérer les commentaires/réponses
    d'un item, quelle que soit la plateforme.
    """
    if platform == "facebook":
        return get_all_facebook_comments(get_post_url(post))

    if platform in ("twitter", "x"):
        tweet_id = post.get("id")
        replies = get_tweet_replies(tweet_id)
        return [tweet_to_comment_like(r) for r in replies]

    return []


def process_item(post, target_name, target_url, platform, pending, target_data):
    """
    Applique la logique de seuil (SEUIL_COMMENTAIRES) à un post Facebook
    OU un tweet normalisé, de façon identique.
    """
    post_id = get_post_id(post)
    post_url = get_post_url(post)
    post_date = get_post_date(post)

    print()
    print(f"   📝 Item : {post_id or 'ID inconnu'}")
    print(f"      URL : {post_url}")
    print(f"      Date : {post_date.isoformat() if post_date else 'inconnue'}")

    if not post_date:
        print("      ⚠️ Date inconnue → ignoré")
        return

    if post_date < cutoff_24h():
        print("      🚫 > 24h → ignoré")
        return

    if not post_url:
        print("      ⚠️ Pas d'URL → ignoré")
        return

    post["_target_url"] = target_url

    comment_count = post.get("commentCount", 0)
    if comment_count is None:
        comment_count = 0
    try:
        comment_count = int(comment_count)
    except Exception:
        comment_count = 0

    print(f"      💬 Compteur annoncé : {comment_count}")

    # ------------------------------------------------------------
    # NIVEAU 1 : sous le seuil -> pending
    # ------------------------------------------------------------
    if comment_count < SEUIL_COMMENTAIRES:
        print(f"      ⏳ < {SEUIL_COMMENTAIRES} → pending")
        add_to_pending(pending, post, target_name, platform, target_url)
        return

    # ------------------------------------------------------------
    # NIVEAU 2 : seuil atteint -> on va chercher le détail
    # ------------------------------------------------------------
    print(f"      🎯 Seuil de {SEUIL_COMMENTAIRES} atteint.")
    comments = fetch_comments_for(platform, post)
    print(f"      📥 Total récupéré : {len(comments)} commentaire(s)/réponse(s)")

    if len(comments) < SEUIL_COMMENTAIRES:
        print(f"      ⚠️ Seulement {len(comments)} réellement récupéré(s).")
        print("      ⏳ Conservation en pending.")
        add_to_pending(pending, post, target_name, platform, target_url)
        return

    # ------------------------------------------------------------
    # NIVEAU 3 : validé -> corpus
    # ------------------------------------------------------------
    print("      ✅ Item validé → ajout au corpus")

    detail_key = "post_details" if platform == "facebook" else "tweet_details"

    target_data["posts_collectes"].append({
        detail_key: post,
        "comments_count": len(comments),
        "comments": comments
    })

    remove_from_pending(pending, post_id or post_url)


# ============================================================
# RETEST DES PENDING (générique, toutes plateformes)
# ============================================================

def get_or_create_target_data(corpus, target_name, platform, target_url):
    for target in corpus:
        if target.get("target_name") == target_name and target.get("platform") == platform:
            return target

    target_data = {
        "target_name": target_name,
        "platform": platform,
        "target_url": target_url,
        "posts_collectes": []
    }
    corpus.append(target_data)
    return target_data


def commit_pending_item_to_corpus(corpus, item, post, comments, current_count):
    target_name = item.get("target_name", "Unknown")
    target_url = item.get("target_url") or post.get("_target_url")
    platform = item.get("platform", "facebook")

    post["_target_url"] = target_url

    target_data = get_or_create_target_data(corpus, target_name, platform, target_url)
    detail_key = "post_details" if platform == "facebook" else "tweet_details"

    target_data["posts_collectes"].append({
        detail_key: post,
        "comments_count": current_count,
        "comments": comments
    })


def process_pending(pending, corpus):
    """
    Reteste chaque item en attente.

    Règles :
      - >= SEUIL_COMMENTAIRES (10) commentaires  -> pris, retiré du pending
      - < SEUIL_COMMENTAIRES et pending depuis moins de PENDING_MAX_DAYS (3j)
            -> reste en pending, on retente au prochain run
      - < SEUIL_COMMENTAIRES et pending depuis >= PENDING_MAX_DAYS (3j) :
            - >= SEUIL_COMMENTAIRES_APRES_DELAI (5) -> pris quand même, retiré
            - sinon -> abandonné, retiré définitivement (pas ajouté au corpus)

    L'ancienneté est mesurée sur 'added_at' (date d'ajout au pending),
    PAS sur la date de publication du post/tweet.
    """
    if not pending:
        print("⏳ Aucun post en attente.")
        return

    print()
    print("====================================================")
    print("🔄 RETEST DES POSTS EN ATTENTE")
    print("====================================================")

    for item in list(pending):
        post = item.get("post", {})
        post_id = item.get("post_id") or get_post_id(post)
        post_url = item.get("post_url") or get_post_url(post)
        target_name = item.get("target_name", "Unknown")
        platform = item.get("platform", "facebook")

        print()
        print(f"🔄 Pending : {post_id or post_url}")
        print(f"   🎯 Cible : {target_name} ({platform})")
        print(f"   🔗 URL : {post_url}")

        if not post_url:
            print("   ⚠️ URL absente → suppression du pending")
            remove_from_pending(pending, pending_key(item))
            continue

        added_at = parse_datetime(item.get("added_at")) or now_utc()
        age = now_utc() - added_at
        age_days = age.total_seconds() / 86400
        delai_ecoule = age >= timedelta(days=PENDING_MAX_DAYS)

        print(f"   🕐 En pending depuis {age_days:.1f} jour(s) "
              f"({'délai écoulé' if delai_ecoule else f'< {PENDING_MAX_DAYS}j'})")

        print(f"   💬 Appel Sociavault ({platform})")
        comments = fetch_comments_for(platform, post)
        current_count = len(comments)
        print(f"   📊 Commentaires actuellement récupérés : {current_count}")

        # ------------------------------------------------------------
        # CAS 1 : seuil normal atteint -> pris, quel que soit l'âge
        # ------------------------------------------------------------
        if current_count >= SEUIL_COMMENTAIRES:
            print(f"   🎯 Seuil de {SEUIL_COMMENTAIRES} atteint ! Déplacement vers le corpus")
            commit_pending_item_to_corpus(corpus, item, post, comments, current_count)
            remove_from_pending(pending, pending_key(item))
            print("   ✅ Post retiré de pending")
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            continue

        # ------------------------------------------------------------
        # CAS 2 : sous le seuil, délai de 3 jours pas encore écoulé
        # -> on patiente
        # ------------------------------------------------------------
        if not delai_ecoule:
            print(f"   ⏳ Toujours < {SEUIL_COMMENTAIRES}, délai pas écoulé → reste en pending")
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            continue

        # ------------------------------------------------------------
        # CAS 3 : délai de 3 jours écoulé
        # ------------------------------------------------------------
        if current_count >= SEUIL_COMMENTAIRES_APRES_DELAI:
            print(f"   🎯 Délai écoulé mais seuil réduit ({SEUIL_COMMENTAIRES_APRES_DELAI}) "
                  f"atteint → pris quand même")
            commit_pending_item_to_corpus(corpus, item, post, comments, current_count)
        else:
            print(f"   🗑️ Délai écoulé et < {SEUIL_COMMENTAIRES_APRES_DELAI} commentaires "
                  f"→ abandonné définitivement")

        remove_from_pending(pending, pending_key(item))
        time.sleep(SLEEP_BETWEEN_REQUESTS)


# ============================================================
# TRAITEMENT PAR CIBLE
# ============================================================

def process_facebook_target(target, pending, corpus):
    name = target.get("name", "Sans nom")
    url = target.get("url")

    print()
    print("----------------------------------------------------")
    print(f"📘 FACEBOOK : {name}")
    print(f"   URL : {url}")

    if not url:
        print("   ❌ URL Facebook absente")
        return

    target_data = {
        "target_name": name,
        "platform": "facebook",
        "target_url": url,
        "posts_collectes": []
    }

    posts = get_facebook_recent_posts(url)
    print(f"\n   📄 {len(posts)} post(s) de moins de 24h.")

    for post in posts:
        process_item(
            post=post,
            target_name=name,
            target_url=url,
            platform="facebook",
            pending=pending,
            target_data=target_data
        )
        save_pending(pending)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    if target_data["posts_collectes"]:
        corpus.append(target_data)


def process_twitter_target(target, pending, corpus):
    name = target.get("name", "Sans nom")
    url = target.get("url")
    platform = str(target.get("platform", "twitter")).lower()

    print()
    print("----------------------------------------------------")
    print(f"🐦 TWITTER / X : {name}")
    print(f"   URL : {url}")

    if not url:
        print("   ❌ URL Twitter absente")
        return

    target_data = {
        "target_name": name,
        "platform": platform,
        "target_url": url,
        "posts_collectes": []
    }

    raw_tweets = get_twitter_recent_tweets(url)
    print(f"\n   📄 {len(raw_tweets)} tweet(s) récupéré(s) au total (avant filtre 24h).")

    tweets = [normalize_tweet(t) for t in raw_tweets]
    tweets = [t for t in tweets if t.get("creation_time")]
    tweets.sort(key=lambda t: get_post_date(t) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    for tweet in tweets:
        process_item(
            post=tweet,
            target_name=name,
            target_url=url,
            platform=platform,
            pending=pending,
            target_data=target_data
        )
        save_pending(pending)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    if target_data["posts_collectes"]:
        corpus.append(target_data)


# ============================================================
# MAIN
# ============================================================

def main():
    print()
    print("====================================================")
    print("🚀 SOCIAVAULT COLLECTOR")
    print("====================================================")
    print(f"Fenêtre : dernières {WINDOW_HOURS}h")
    print(f"Seuil commentaires : {SEUIL_COMMENTAIRES}")
    print("====================================================")

    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(YAML_PATH):
        targets = load_targets(YAML_PATH)
    else:
        alternative_yaml = os.path.join(DATA_DIR, "targets.yaml")
        targets = load_targets(alternative_yaml) if os.path.exists(alternative_yaml) else []

    print(f"\n🎯 {len(targets)} cible(s) trouvée(s).")

    pending = load_pending()
    print(f"⏳ {len(pending)} post(s) déjà en attente.")

    corpus = []

    # 1. Retest des pending (Facebook + Twitter mélangés)
    process_pending(pending, corpus)
    save_pending(pending)
    save_corpus(corpus)

    # 2. Nouvelle collecte
    for idx, target in enumerate(targets, 1):
        name = target.get("name", f"Cible {idx}")
        platform = str(target.get("platform", "")).lower()

        print()
        print("----------------------------------------------------")
        print(f"🎯 {name} ({platform})")

        if platform == "facebook":
            process_facebook_target(target, pending, corpus)
        elif platform in ("twitter", "x"):
            process_twitter_target(target, pending, corpus)
        else:
            print(f"⚠️ Plateforme inconnue : {platform}")

        save_pending(pending)
        save_corpus(corpus)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    save_pending(pending)
    save_corpus(corpus)

    total_posts = sum(len(t.get("posts_collectes", [])) for t in corpus)

    print()
    print("====================================================")
    print("🎉 COLLECTE TERMINÉE")
    print("====================================================")
    print(f"📄 Posts/tweets dans le corpus : {total_posts}")
    print(f"⏳ Posts en attente : {len(pending)}")
    print(f"💾 Corpus : {OUTPUT_JSON}")
    print(f"💾 Pending : {PENDING_FILE}")
    print("====================================================")


if __name__ == "__main__":
    main()