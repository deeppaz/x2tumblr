"""X (@miniocular) media timeline -> Tumblr. Her çalışmada kuyruktan 1 tweet'i post eder."""
import io
import json
import os
import random
import sys
import time

import imagehash
import requests
from PIL import Image
from requests_oauthlib import OAuth1
from gallery_dl import config, extractor
from gallery_dl.extractor.message import Message
from gallery_dl.extractor.twitter import TwitterAPI

X_USER = os.environ.get("X_USER", "miniocular")
BLOG = os.environ.get("TUMBLR_BLOG", "")
# İlk 5 tag tracked-tag'lerde görünür -> sabit ve en güçlüler. Kalanlar havuzdan rastgele (spam gibi durmasın diye).
CORE_TAGS = ["aesthetic", "moodboard", "quotes", "art", "mood"]
TAG_POOL = [
    "aesthetic blog", "mood aesthetic", "dark aesthetic", "grunge", "soft grunge", "collage", "collage art",
    "text post", "words", "feelings", "vibes", "art blog", "digital art", "surreal", "neon", "alt aesthetic",
    "life quotes", "thoughts", "wallpaper", "lockscreen", "tumblr aesthetic", "90s aesthetic", "graphic design",
]
EXTRA_TAGS = 7
DUP_DISTANCE = 6  # phash hamming mesafesi; bunun altı = aynı görsel sayılır
FETCH_TWEETS = int(os.environ.get("FETCH_TWEETS", "40"))  # her çalışmada taranacak son tweet sayısı
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# Cron saatlik çalışır (GitHub zamanlanmış run'ları atlayabiliyor); son post'tan bu kadar geçmediyse çık.
MIN_INTERVAL_H = float(os.environ.get("MIN_INTERVAL_H", "3.75"))
FORCE = os.environ.get("FORCE") == "1"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
IMAGE_EXT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    state.setdefault("seen", [])      # kuyruğa alınmış tüm tweet id'leri
    state.setdefault("queue", [])     # {tweet_id, url, images:[{url, ext}]}
    state.setdefault("posted", [])    # {tweet_id, tumblr_id}
    state.setdefault("blocked", [])   # DMCA vs. gelirse buraya tweet_id ekle, asla post edilmez
    state.setdefault("hashes", [])    # post edilmiş görsellerin perceptual hash'leri
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


class _NoTransaction:
    def generate_transaction_id(self, method, path):
        return None


_client_transaction = TwitterAPI._client_transaction


def _client_transaction_or_none(self):
    # gallery-dl transaction key'leri için x.com/home'u çekiyor; GitHub IP'lerinde Cloudflare 403 dönüyor.
    # Alınamazsa header'sız devam et, UserMedia uç noktası yine kabul ediyor.
    try:
        return _client_transaction(self)
    except Exception as e:
        print(f"transaction-id devre dışı: {e!r}", file=sys.stderr)
        return _NoTransaction()


TwitterAPI._client_transaction = _client_transaction_or_none


def fetch_recent():
    """Son FETCH_TWEETS tweet'in görsellerini döner (en yeni önce)."""
    config.set(("extractor", "twitter"), "cookies", {"auth_token": os.environ["X_AUTH_TOKEN"]})
    config.set(("extractor", "twitter"), "videos", False)
    config.set(("extractor", "twitter"), "retweets", False)

    ex = extractor.find(f"https://x.com/{X_USER}/media")
    tweets = {}
    for msg in ex:
        if msg[0] != Message.Url:
            continue
        url, kw = msg[1], msg[2]
        ext = kw.get("extension", "").lower()
        if ext not in IMAGE_EXT:
            continue
        tid = str(kw["tweet_id"])
        if tid not in tweets:
            if len(tweets) >= FETCH_TWEETS:
                break
            author = (kw.get("author") or {}).get("name") or X_USER
            tweets[tid] = {"tweet_id": tid, "url": f"https://x.com/{author}/status/{tid}", "images": []}
        tweets[tid]["images"].append({"url": url, "ext": ext})
    return list(tweets.values())


def pick_tags():
    env = [t.strip() for t in os.environ.get("TUMBLR_TAGS", "").split(",") if t.strip()]
    return env or CORE_TAGS + random.sample(TAG_POOL, EXTRA_TAGS)


def is_dup(h, known):
    return any(h - k <= DUP_DISTANCE for k in known)


def post_to_tumblr(item, state):
    auth = OAuth1(
        os.environ["TUMBLR_CONSUMER_KEY"], os.environ["TUMBLR_CONSUMER_SECRET"],
        os.environ["TUMBLR_OAUTH_TOKEN"], os.environ["TUMBLR_OAUTH_SECRET"],
    )
    known = [imagehash.hex_to_hash(x) for x in state["hashes"]]
    files, media, new_hashes = {}, [], []
    for i, img in enumerate(item["images"][:10]):
        r = requests.get(img["url"], timeout=60)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        h = imagehash.phash(Image.open(io.BytesIO(r.content)).convert("RGB"))
        if is_dup(h, known + new_hashes):
            print(f"tekrar görsel atlandı: {img['url']}")
            continue
        new_hashes.append(h)
        ident = f"img{i}"
        mime = IMAGE_EXT[img["ext"]]
        files[ident] = (f"{ident}.{img['ext']}", r.content, mime)
        media.append({"type": "image", "media": [{"type": mime, "identifier": ident}]})
    if not media:
        return None  # görseller silinmiş ya da hepsi daha önce paylaşılmış

    body = {"content": media, "tags": ",".join(pick_tags())}
    if DRY_RUN:
        print("[dry-run]", json.dumps(body)[:500])
        return "dry-run"

    files["json"] = (None, json.dumps(body), "application/json")
    r = requests.post(f"https://api.tumblr.com/v2/blog/{BLOG}/posts", auth=auth, files=files, timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"Tumblr {r.status_code}: {r.text[:500]}")
    state["hashes"].extend(str(h) for h in new_hashes)
    return str(r.json()["response"].get("id"))


def main():
    state = load_state()
    elapsed_h = (time.time() - state.get("last_post_at", 0)) / 3600
    if not FORCE and elapsed_h < MIN_INTERVAL_H:
        print(f"son post {elapsed_h:.1f} saat önce, bekleniyor")
        return
    seen = set(state["seen"])

    try:
        fresh = [t for t in fetch_recent() if t["tweet_id"] not in seen]
        state["queue"].extend(reversed(fresh))  # eskiden yeniye sırala
        state["seen"].extend(t["tweet_id"] for t in fresh)
        print(f"fetch: {len(fresh)} yeni tweet, kuyruk={len(state['queue'])}")
    except Exception as e:  # X tarafı patlarsa kuyruktan devam et
        print(f"fetch hatası: {e!r}", file=sys.stderr)

    blocked = set(state["blocked"])
    exit_code = 0
    while state["queue"]:
        item = state["queue"][0]
        if item["tweet_id"] in blocked:
            state["queue"].pop(0)
            continue
        try:
            post_id = post_to_tumblr(item, state)
        except Exception as e:
            print(f"post hatası ({item['tweet_id']}): {e}", file=sys.stderr)
            exit_code = 1  # kuyrukta kalır, sonraki çalışmada tekrar denenir
            break
        state["queue"].pop(0)
        if post_id is None:
            print(f"atlandı (görsel yok/tekrar): {item['url']}")
            continue
        if not DRY_RUN:
            state["posted"].append({"tweet_id": item["tweet_id"], "tumblr_id": post_id})
            state["last_post_at"] = int(time.time())
        print(f"post edildi: {item['url']} -> {post_id}")
        break
    else:
        print("kuyruk boş, post yok")

    if not DRY_RUN:
        save_state(state)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
