"""X (@miniocular) media timeline -> Tumblr. Posts one tweet from the queue per run."""
import io
import json
import os
import sys
import time

import imagehash
import requests
from PIL import Image
from requests_oauthlib import OAuth1
from gallery_dl import config, extractor
from gallery_dl.extractor.message import Message
from gallery_dl.extractor.twitter import TwitterAPI

from tags import pick_tags

X_USER = os.environ.get("X_USER", "miniocular")
BLOG = os.environ.get("TUMBLR_BLOG", "")
DUP_DISTANCE = 6  # phash hamming distance; at or below this counts as the same image
FETCH_TWEETS = int(os.environ.get("FETCH_TWEETS", "40"))  # number of latest tweets scanned per run
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# Cron runs hourly (GitHub can skip scheduled runs); exit unless this long has passed since the last post.
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
    state.setdefault("seen", [])      # every tweet id ever queued
    state.setdefault("queue", [])     # {tweet_id, url, images:[{url, ext}]}
    state.setdefault("posted", [])    # {tweet_id, tumblr_id}
    state.setdefault("blocked", [])   # add a tweet_id here on a DMCA etc.; it will never be posted
    state.setdefault("hashes", [])    # perceptual hashes of posted images
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


class _NoTransaction:
    def generate_transaction_id(self, method, path):
        return None


_client_transaction = TwitterAPI._client_transaction


def _client_transaction_or_none(self):
    # gallery-dl fetches x.com/home for transaction keys; Cloudflare returns 403 on GitHub IPs.
    # If that fails, go on without the header; the UserMedia endpoint still accepts the request.
    try:
        return _client_transaction(self)
    except Exception as e:
        print(f"transaction-id devre dışı: {e!r}", file=sys.stderr)
        return _NoTransaction()


TwitterAPI._client_transaction = _client_transaction_or_none


def fetch_recent():
    """Returns the images of the latest FETCH_TWEETS tweets (newest first)."""
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
        return None  # images were deleted or all of them were posted before

    tags = pick_tags([f[1] for f in files.values()])
    print(f"tags: {tags}")
    body = {"content": media, "tags": ",".join(tags)}
    if DRY_RUN:
        print("[dry-run]", json.dumps(body, ensure_ascii=False)[:1500])
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
        state["queue"].extend(reversed(fresh))  # oldest first
        state["seen"].extend(t["tweet_id"] for t in fresh)
        print(f"fetch: {len(fresh)} yeni tweet, kuyruk={len(state['queue'])}")
    except Exception as e:  # if X fails, carry on from the queue
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
            exit_code = 1  # stays queued and is retried on the next run
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
