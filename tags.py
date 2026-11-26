"""Per-post tag selection: Claude looks at the image and proposes candidate tags, then tags that are dead on Tumblr are dropped and the rest are sorted by current activity."""
import base64
import io
import json
import os
import random
import sys
import time

import requests
from PIL import Image

# Broad discovery tags. Offered to the model as "use if it fits"; sampled at random when ANTHROPIC_API_KEY is missing.
BROAD_TAGS = [
    "aesthetic", "moodboard", "quotes", "art", "mood", "aesthetic blog", "mood aesthetic", "dark aesthetic",
    "grunge", "soft grunge", "collage", "collage art", "text post", "words", "feelings", "vibes", "art blog",
    "digital art", "surreal", "neon", "alt aesthetic", "life quotes", "thoughts", "wallpaper", "lockscreen",
    "tumblr aesthetic", "90s aesthetic", "graphic design", "illustration", "photography", "typography",
    "vintage", "retro", "minimalism", "abstract", "pop art", "cute", "love quotes", "poetry", "weirdcore",
    "dreamcore", "vaporwave", "y2k", "nature", "animals", "cats", "film photography", "black and white",
]
FALLBACK_COUNT = 12
MAX_TAGS = 20        # Tumblr search indexes the first 20 tags of a post
CANDIDATES = 26      # candidates requested from the model; cut down to MAX_TAGS after dead tags are dropped
DEAD_AFTER_H = 24 * 30  # a tag whose newest post is older than this has no audience
MODEL = os.environ.get("TAG_MODEL") or "claude-sonnet-5-5"  # the workflow passes an empty string when the variable is unset

PROMPT = f"""These images are one Tumblr post on an aesthetic / moodboard blog that reposts curated images.
Pick the tags that will get this exact post discovered by people who would enjoy it.

Return up to {CANDIDATES} tags, best first, written the way Tumblr users type them: lowercase, spaces allowed, no '#'.
Cover what is actually in the image:
- subject (what or who is shown; name a real person, artwork or franchise only when you are sure)
- medium and style (collage, neon sign, illustration, film photo, typography, ...)
- colour and mood
- if the image contains readable text, tags for its theme (and "quotes" / "text post" style tags only then)
- broad discovery tags from this list, but only the ones that truly describe this post:
  {", ".join(BROAD_TAGS)}

Prefer tags people really browse and follow over invented phrases; a tag nobody else uses is worthless.
The blog owner did not make these images, so never use authorship tags such as "my art", "my photo",
"artists on tumblr", "original content" or "oc"."""

SCHEMA = {
    "type": "object",
    "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
    "required": ["tags"],
    "additionalProperties": False,
}


def _shrink(data):
    """Downscale the image for the vision call (token cost and the 5 MB limit)."""
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((1024, 1024))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode()


def suggest(images):
    """images: raw image bytes. Returns the model's tags, best fit first."""
    import anthropic

    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _shrink(b)}}
        for b in images[:4]
    ]
    content.append({"type": "text", "text": PROMPT})
    r = anthropic.Anthropic(timeout=90).messages.create(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    if r.stop_reason != "end_turn":
        raise RuntimeError(f"model stop_reason={r.stop_reason}")
    return json.loads(next(b.text for b in r.content if b.type == "text"))["tags"]


def clean(tags):
    out = []
    for t in tags:
        t = " ".join(str(t).replace("#", " ").replace(",", " ").lower().split())[:60]
        if t and t not in out:
            out.append(t)
    return out


def activity(tag, api_key):
    """Current posting rate of the tag on Tumblr (posts/hour). 0 = dead, None = could not be measured."""
    try:
        r = requests.get(
            "https://api.tumblr.com/v2/tagged", params={"tag": tag, "api_key": api_key}, timeout=15
        )
        r.raise_for_status()
        stamps = [p["timestamp"] for p in r.json()["response"] if "timestamp" in p]
    except Exception as e:
        # Do not print e itself: requests puts the URL with api_key in the message, and logs are public on a public repo
        print(f"could not measure tag ({tag}): {type(e).__name__}", file=sys.stderr)
        return None
    now = time.time()
    print(f"tagged {tag!r}: n={len(stamps)} ages_h={[round((now - t) / 3600, 1) for t in sorted(stamps, reverse=True)[:3]]}..{round((now - min(stamps)) / 3600, 1) if stamps else None} keys={sorted(r.json()['response'][0])[:40] if r.json()['response'] else r.text[:200]}")
    if not stamps or (now - max(stamps)) / 3600 > DEAD_AFTER_H:
        return 0.0
    return len(stamps) / max((now - min(stamps)) / 3600, 0.01)


def rank(tags, api_key):
    """Drop dead tags and sort the rest by activity (unmeasured ones stay last, in the model's order)."""
    scored = [(t, activity(t, api_key)) for t in tags]
    alive = [(t, a) for t, a in scored if a != 0]
    dropped = [t for t, a in scored if a == 0]
    if dropped:
        print(f"dropped dead tags: {dropped}")
    alive.sort(key=lambda x: -(x[1] or 0))  # stable sort: ties keep the model's order
    return [t for t, _ in alive]


def fallback():
    return BROAD_TAGS[:5] + random.sample(BROAD_TAGS[5:], FALLBACK_COUNT - 5)


def pick_tags(images):
    env = clean(os.environ.get("TUMBLR_TAGS", "").split(","))
    if env:
        return env
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return fallback()
    try:
        tags = clean(suggest(images))[:CANDIDATES]
    except Exception as e:
        print(f"tag suggestion failed, falling back to the static pool: {e!r}", file=sys.stderr)
        return fallback()
    api_key = os.environ.get("TUMBLR_CONSUMER_KEY")
    if api_key:
        tags = rank(tags, api_key)
    return tags[:MAX_TAGS] or fallback()
