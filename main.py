import os
import logging
import json
import random
import asyncio
from pathlib import Path
import shutil
from datetime import datetime, timezone
import sys
from drive import GoogleDriveClient
from dotenv import load_dotenv

from pydoll.browser.chromium.chrome import Chrome
load_dotenv()


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("uploader")

BASE = "https://www.pixiv.net"
LOGIN_URL = f"https://accounts.pixiv.net/login?lang=en"
UPLOAD_URL = f"{BASE}/illustration/create"
DESCRIPTION = "https://patreon.com/MaximusOC"


POSTS_FILE = Path("posts.json")
STATE_FILE = Path("storageState.json")
TMP_DIR = Path("tmp_downloads")
DEBUG_DIR = Path("debug")

RATING_MAP = {"safe": "general", "questionable": "general", "explicit": "r18"}

DESCRIPTION = """Subscribe to my Patreon 🔞 for exclusive content: 
https://patreon.com/maximusoc

Follow me on:
Twitter: https://twitter.com/maximusoc
DeviantArt: https://www.deviantart.com/maximusoc
"""

# Selectors, adjust here if the site markup changes
SEL = {
    "login_user": 'input[placeholder="E-mail address or pixiv ID"]',
    "login_pass": 'input[placeholder="Password"]',
    "login_submit": 'button[type="submit"][data-variant="Primary"]',
    "file": 'input[type="file"][name="files[]"]',
    "description": 'textarea[name="comment"]',
    "title": 'input[name="title"]',
    "rating": lambda r: f'input[name="x_restrict"][value="{r}"]',
    "tags": 'input[placeholder="Tags"]',
    "ai_generated": 'input[name="ai_type"][value="aiGenerated"]',
    "sexual": 'input[name="sexual"][value="true"]',
    "is_original": 'input[type="checkbox"][name="original"]',
    "submit": 'button[class="charcoal-button"][data-variant="Primary"]',
}

# ---------- queue ----------
def load_posts() -> list[dict]:
    return json.loads(POSTS_FILE.read_text(encoding="utf-8"))

def save_posts(posts: list[dict]) -> None:
    POSTS_FILE.write_text(json.dumps(posts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def pending(posts: list[dict]) -> list[dict]:
    """Only items that were never posted AND never failed. Failed items are never retried."""
    return [p for p in posts if not p.get("posted") and not p.get("failed")]


def mark_posted(posts: list[dict], post: dict) -> None:
    post["posted"] = True
    post["posted_at"] = datetime.now(timezone.utc).isoformat()
    for k in ("failed", "failed_at", "error"):
        post.pop(k, None)
    save_posts(posts)


def mark_failed(posts: list[dict], post: dict, reason: str) -> None:
    if post.get("posted"):
        return  # never downgrade a successful post
    post["failed"] = True
    post["failed_at"] = datetime.now(timezone.utc).isoformat()
    post["error"] = reason[:500]
    save_posts(posts)


# ---------- helpers ----------
async def human_pause(lo=0.6, hi=1.8):
    await asyncio.sleep(random.uniform(lo, hi))


async def type_into(el, text: str, fast: bool = False):
    await el.click()
    await human_pause(0.2, 0.6)
    if fast:
        await el.insert_text(text)
        return
    try:
        await el.type_text(text, humanize=True)
    except TypeError:
        await el.type_text(text, interval=random.uniform(0.04, 0.12))


async def snap(tab, name: str):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        await tab.take_screenshot(path=str(DEBUG_DIR / f"{name}.png"))
    except Exception as e:  # noqa
        log.warning("Screenshot failed: %s", e)


# ---------- session persistence ----------
COOKIE_KEYS = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}


async def save_state(browser, tab):
    cookies = await browser.get_cookies()
    clean = [{k: v for k, v in c.items() if k in COOKIE_KEYS} for c in cookies]
    try:
        raw = await tab.execute_script(
            "return JSON.stringify(Object.fromEntries(Object.entries(localStorage)))"
        )
        local = json.loads(raw["result"]["result"]["value"])
    except Exception:
        local = {}
    STATE_FILE.write_text(
        json.dumps({"cookies": clean, "localStorage": local, "origin": BASE}, indent=2),
        encoding="utf-8",
    )
    log.info("Session state saved (%d cookies)", len(clean))


async def load_state(browser, tab) -> bool:
    if not STATE_FILE.exists():
        return False
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if state.get("cookies"):
            await browser.set_cookies(state["cookies"])
        if state.get("localStorage"):
            await tab.go_to(BASE)
            payload = json.dumps(state["localStorage"])
            await tab.execute_script(
                f"Object.entries({payload}).forEach(([k,v]) => localStorage.setItem(k,v));"
            )
        log.info("Session state loaded")
        return True
    except Exception as e:
        log.warning("Could not load state: %s", e)
        return False

# ---------- auth ----------
async def is_logged_in(tab) -> bool:
    await tab.go_to(UPLOAD_URL)
    await human_pause(1.5, 3)
    el = await tab.query(SEL["file"], timeout=5, raise_exc=False)
    return el is not None


async def login(tab):
    log.info("Logging in...")
    await tab.go_to(LOGIN_URL)
    await human_pause(1.5, 3)
    user = await tab.query(SEL["login_user"], timeout=15)
    pw = await tab.query(SEL["login_pass"], timeout=15)
    await type_into(user, os.environ["PIXIV_USERNAME"])
    await human_pause()
    await type_into(pw, os.environ["PIXIV_PASSWORD"])
    await human_pause()
    
    await snap(tab, "upload_form")
    
    btn = await tab.query(SEL["login_submit"], timeout=10)
    await btn.click()
    await human_pause(2.5, 4.5)
    if not await is_logged_in(tab):
        await snap(tab, "login_failed")
        raise RuntimeError("Login failed (bad credentials, captcha, or markup change)")
    log.info("Login OK")

def sanitize_tags(raw_tags: str) -> str:
    # Pixiv doesn't allow commas in tags, so replace them with spaces
    keywords = raw_tags.replace(",", " ").split()
    count = len(keywords)
    if count > 10:
        log.warning("Too many tags (%d), truncating to 10", count)
        keywords = keywords[:10]
    return " ".join(keywords)

async def upload_post(tab, post: dict, image_path: Path):
    x_rating = RATING_MAP.get(post["rating"].lower())
    if not x_rating:
        raise ValueError(f"Invalid rating: {post['rating']}")
    
    await tab.go_to(UPLOAD_URL)
    await human_pause(1.5, 3)
    
    file_input = await tab.query(SEL["file"], timeout=15)
    await file_input.set_input_files(str(image_path.resolve()))
    await human_pause()
    
    rating = await tab.query(SEL["rating"](x_rating), timeout=10)
    await rating.click()
    await human_pause(0.4, 1.0)
    
    rating_re = await tab.query(SEL["rating"](x_rating), timeout=10)
    await rating_re.click()
    await human_pause(0.4, 1.0)
    
    if x_rating == "general":
        sexual = await tab.query(SEL["sexual"], timeout=10)
        await sexual.click()
        await human_pause(0.4, 1.0)


    title = await tab.query(SEL["title"], timeout=10, raise_exc=False)
    if title and post.get("title"):
        await type_into(title, post["title"])
        await human_pause(0.4, 1.0)
        
    desc = await tab.query(SEL["description"], timeout=10)
    await desc.insert_text("")  # ensure focus path is valid
    await type_into(desc, DESCRIPTION, fast=True)
    await type_into(desc, " ")
    await human_pause(0.4, 1.0)

    tags = await tab.query(SEL["tags"], timeout=10)

    clean_tags = sanitize_tags(post["pixiv_tag"])
    await type_into(tags, clean_tags)
    await human_pause(1, 2)
    
    ai_generated = await tab.query(SEL["ai_generated"], timeout=10)
    await ai_generated.click()
    await human_pause(0.5, 1.0)
    
    checkbox = await tab.query(SEL["is_original"], timeout=10)
    await checkbox.click()
    await human_pause(0.4, 1.0)

    submit = await tab.query(SEL["submit"], timeout=10)
    await submit.click()
    await human_pause(4, 7)
    
    url = await tab.current_url()
    html = (await tab.page_source()).lower()
    if "users" in url or "Your work was posted" in html:
        log.info("Upload accepted. URL=%s", url)
        return True
    if "already exists" in html or "error" in html:
        log.error("Site reported an error/duplicate. URL=%s", url)
    await snap(tab, f"upload_failed_{image_path.stem}")
    return False


async def main():
    posts = load_posts()
    queue = pending(posts)
    if not queue:
        log.info("Nothing to post.")
        return

    batch = queue[:1]
    log.info("%d pending, processing %d this run", len(queue), len(batch))

    drive = GoogleDriveClient(os.environ["GDRIVE_FOLDER_ID"])
    TMP_DIR.mkdir(exist_ok=True)
    downloaded_files = []
    attempted = 0

    async with Chrome() as browser:
        tab = await browser.start()
        await load_state(browser, tab)
        if not await is_logged_in(tab):
            await login(tab)  # login failure aborts the run; no post is touched or marked
        await save_state(browser, tab)

        for i, post in enumerate(batch):
            log.info("[%d/%d] %s", i + 1, len(batch), post["filename"])
            local = None

            # Clear temp downloads before each task to avoid stale file interference
            shutil.rmtree(TMP_DIR, ignore_errors=True)
            TMP_DIR.mkdir(exist_ok=True)

            # Mark as failed BEFORE trying. If the process is killed mid-upload
            # (timeout, crash, cancel) the item will never be retried, which
            # prevents duplicate posts. Overwritten by mark_posted on success.
            mark_failed(posts, post, "interrupted before completion")
            attempted += 1

            try:
                local = drive.download(post["filename"], str(TMP_DIR))
                downloaded_files.append(local)
                ok = await upload_post(tab, post, local)
                if ok:
                    mark_posted(posts, post)  # persist immediately
                    log.info("Posted: %s", post["filename"])
                    try:
                        await save_state(browser, tab)
                    except Exception as e:
                        log.warning("Could not save session state: %s", e)
                else:
                    mark_failed(posts, post, "Site rejected the upload (error/duplicate/unknown)")
                    log.error("Failed (will not retry): %s", post["filename"])
            except Exception as e:
                log.exception("Error on %s: %s", post["filename"], e)
                mark_failed(posts, post, f"{type(e).__name__}: {e}")

    for local in downloaded_files:
        if local.exists():
            local.unlink(missing_ok=True)
    shutil.rmtree(TMP_DIR, ignore_errors=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
