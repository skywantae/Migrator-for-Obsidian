"""
네이버 블로그 -> 옵시디언(Obsidian) 마이그레이션 도구 (GUI)
==========================================================

- 창에서 저장 위치를 폴더 찾아보기로 고르고, 옵션을 체크한 뒤 [시작하기]를 누르면 됩니다.
- 블로그 아이디는 로그인한 계정에서 자동으로 알아냅니다.
- 크롬 창은 로그인 세션(쿠키)을 얻기 위해서만 잠깐 뜨고, 이후 수집은 전부 HTTP로 이뤄집니다.

수집 방식 (Obsidian "Naver Blog Importer" 플러그인과 동일한 고속 방식):
    목록   PostTitleListAsync.naver  (JSON, 30개씩)
    본문   PostView.naver?...&redirect=Dlog&widgetTypeCall=true  (HTML 직접 수신)
    카테고리 m.blog.naver.com/api/blogs/{blogId}/category-list
    태그   BlogTagListInfo.naver  (여러 글 묶어서 한 번에)
"""

import json
import math
import os
import queue
import re
import sys
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, unquote_plus

import requests
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from selenium import webdriver
from selenium.common.exceptions import WebDriverException

# ============================== 설정값 ==============================
DEFAULT_SUBFOLDER = "네이버블로그"
LOGIN_TIMEOUT = 300          # 로그인 대기 최대 시간(초). 로그인되면 즉시 진행
COUNT_PER_PAGE = 30
MAX_LIST_PAGES = 200
IMAGE_WORKERS = 8
REQUEST_TIMEOUT = 20

MIN_SHARED_TAGS = 1
RELATED_MAX_LINKS = 5

TEXT_SAMPLE_LENGTH = 1200
NGRAM_SIZES = (2, 3)
SIMILARITY_THRESHOLD = 0.15
SIMILAR_MAX_LINKS = 5
DF_MAX_RATIO = 0.6

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://blog.naver.com/",
}

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "settings.json"

SELECTORS = {
    "title": ["div.se-module.se-title-text", ".se-title-text", ".pcol1", "h3.se_textarea", "#title_1"],
    "date": [".se_publishDate", ".blog_date", ".se-module-date", ".date", ".pcol2"],
    "content": ["div.se-main-container", "div#postViewArea", "div.post-view"],
    # 태그는 BlogTagListInfo API를 우선 사용. (.wrap_tag a 같은 넓은 셀렉터는
    #  태그 입력창의 '취소/확인' 버튼까지 잡히므로 쓰지 않는다)
    "tags": [".area_tag a.itemTagfont", ".post_tag a", "a.itemTagfont"],
}


class Stopped(Exception):
    """사용자가 중단을 요청했을 때 발생."""


# ============================== 볼트 탐지 ==============================
def detect_obsidian_vaults() -> list:
    """설치된 옵시디언의 obsidian.json에서 볼트 경로 목록을 찾는다 (최근에 연 순서)."""
    config = Path(os.environ.get("APPDATA", "")) / "obsidian" / "obsidian.json"
    if not config.exists():
        return []
    try:
        vaults = json.loads(config.read_text(encoding="utf-8")).get("vaults", {})
    except (OSError, json.JSONDecodeError):
        return []

    ordered = sorted(vaults.values(), key=lambda v: (bool(v.get("open")), v.get("ts", 0)), reverse=True)
    return [Path(v["path"]) for v in ordered if v.get("path")]


def default_output_dir() -> Path:
    vaults = detect_obsidian_vaults()
    if vaults:
        return vaults[0] / DEFAULT_SUBFOLDER
    return BASE_DIR / "obsidian_export"


# ============================== 로그인 ==============================
def detect_my_blog_id(driver) -> str:
    """로그인된 계정의 블로그 아이디를 알아낸다. MyBlog.naver 는 내 블로그로 리다이렉트된다."""
    driver.get("https://blog.naver.com/MyBlog.naver")
    time.sleep(2)

    current = driver.current_url
    m = re.search(r"blog\.naver\.com/([A-Za-z0-9_-]+)", current)
    if m and m.group(1) not in ("MyBlog.naver", "PostList.naver"):
        return m.group(1)

    m = re.search(r"blogId=([A-Za-z0-9_-]+)", current)
    if m:
        return m.group(1)

    m = re.search(r'blogId["\s:=]+([A-Za-z0-9_-]+)', driver.page_source)
    if m:
        return m.group(1)
    return ""


def login_and_get_session(log, should_stop):
    """크롬을 띄워 사용자가 직접 로그인하게 하고, 쿠키와 블로그 아이디를 얻는다."""
    log("크롬 창을 엽니다. 네이버에 직접 로그인해 주세요.")
    log("(로그인하면 자동으로 다음 단계로 넘어갑니다)")

    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=options)

    session = requests.Session()
    session.headers.update(HEADERS)
    blog_id = ""

    try:
        driver.get("https://nid.naver.com/nidlogin.login")

        deadline = time.time() + LOGIN_TIMEOUT
        logged_in = False
        last_notice = 0

        while time.time() < deadline:
            if should_stop():
                raise Stopped()
            time.sleep(1.5)
            try:
                names = {c["name"] for c in driver.get_cookies()}
            except WebDriverException:
                raise RuntimeError("크롬 창이 닫혔습니다. 다시 시도해 주세요.")

            if "NID_AUT" in names and "NID_SES" in names:
                logged_in = True
                break

            remaining = int(deadline - time.time())
            if remaining % 15 == 0 and remaining != last_notice:
                last_notice = remaining
                log(f"로그인 대기 중... (남은 시간 {remaining}초)")

        if not logged_in:
            raise RuntimeError("로그인이 확인되지 않았습니다. 다시 시도해 주세요.")

        log("로그인 확인됨.")
        blog_id = detect_my_blog_id(driver)
        if not blog_id:
            raise RuntimeError("블로그 아이디를 알아내지 못했습니다.")
        log(f"블로그 아이디: {blog_id}")

        for cookie in driver.get_cookies():
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"))
    finally:
        driver.quit()

    return session, blog_id


# ============================== 수집 ==============================
def _load_naver_json(text: str) -> dict:
    """네이버 응답은 XSSI 접두사와 잘못된 이스케이프가 섞여 있어 정제 후 파싱한다."""
    text = text.lstrip()
    if text.startswith(")]}'"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[4:]
    text = re.sub(r'\\(?!["\\/bfnrtu])', "", text)
    return json.loads(text)


def fetch_category_names(session, blog_id: str, log) -> dict:
    url = f"https://m.blog.naver.com/api/blogs/{blog_id}/category-list"
    try:
        resp = session.get(url, headers={"Referer": f"https://m.blog.naver.com/{blog_id}"},
                           timeout=REQUEST_TIMEOUT)
        data = _load_naver_json(resp.text)
    except Exception as e:
        log(f"[경고] 카테고리 이름을 가져오지 못했습니다: {e}")
        return {}

    categories = {str(item["categoryNo"]): item["categoryName"]
                  for item in data.get("result", {}).get("mylogCategoryList", [])}
    log(f"카테고리 {len(categories)}개 확인")
    return categories


def collect_posts(session, blog_id: str, max_posts: int, log, should_stop) -> list:
    log("글 목록을 수집합니다...")
    posts = []
    seen = set()

    for page in range(1, MAX_LIST_PAGES + 1):
        if should_stop():
            raise Stopped()

        url = (
            f"https://blog.naver.com/PostTitleListAsync.naver?blogId={blog_id}"
            f"&currentPage={page}&categoryNo=0&parentCategoryNo=&countPerPage={COUNT_PER_PAGE}"
        )
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        data = _load_naver_json(resp.text)

        post_list = data.get("postList", [])
        if not post_list:
            break

        if page == 1 and data.get("totalCount"):
            log(f"네이버가 알려준 전체 글 수: {data['totalCount']}개")

        new_count = 0
        for item in post_list:
            logno = str(item["logNo"])
            if logno in seen:
                continue
            seen.add(logno)
            posts.append({
                "logNo": logno,
                "title": unquote_plus(item.get("title", "")).strip() or "제목없음",
                "date": parse_date(item.get("addDate", "")),
                "category_no": str(item.get("categoryNo", "")),
                "url": f"https://blog.naver.com/{blog_id}/{logno}",
            })
            new_count += 1

        if new_count == 0:
            break
        if max_posts and len(posts) >= max_posts:
            posts = posts[:max_posts]
            break

    log(f"글 {len(posts)}개 수집 완료")
    return posts


def fetch_tags(session, blog_id: str, lognos: list) -> dict:
    """태그 API로 여러 글의 해시태그를 한 번에 가져온다. {logNo: [태그, ...]}"""
    tag_map = {}
    for i in range(0, len(lognos), 50):
        chunk = lognos[i:i + 50]
        url = (
            f"https://blog.naver.com/BlogTagListInfo.naver?blogId={blog_id}"
            f"&logNoList={','.join(chunk)}&logType=mylog"
        )
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            data = _load_naver_json(resp.text)
        except Exception:
            continue
        for item in data.get("taglist", []):
            tags = [t.strip() for t in unquote_plus(item.get("tagName", "")).split(",") if t.strip()]
            if tags:
                tag_map[str(item.get("logno"))] = tags
    return tag_map


def fetch_post_html(session, blog_id: str, logno: str) -> str:
    """본문을 HTTP로 직접 받아온다 (브라우저/iframe 불필요)."""
    url = (
        f"https://blog.naver.com/PostView.naver?blogId={blog_id}"
        f"&logNo={logno}&redirect=Dlog&widgetTypeCall=true"
    )
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def select_first(soup, selectors: list):
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            return el
    return None


def parse_post(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    container = select_first(soup, SELECTORS["content"])
    if container is None:
        raise RuntimeError("본문 영역을 찾을 수 없습니다 (접근 권한 없음).")

    title_el = select_first(soup, SELECTORS["title"])
    date_el = select_first(soup, SELECTORS["date"])

    tags = []
    for sel in SELECTORS["tags"]:
        els = soup.select(sel)
        if els:
            tags = [e.get_text(strip=True).lstrip("#") for e in els if e.get_text(strip=True)]
            break

    return {
        "title": title_el.get_text(strip=True) if title_el else "",
        "date": parse_date(date_el.get_text(strip=True)) if date_el else "",
        "tags": tags,
        "container": container,
    }


# ============================== 변환/저장 ==============================
def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:120]


def parse_date(raw_date: str) -> str:
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", raw_date or "")
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    return ""


def yaml_escape(text: str) -> str:
    return text.replace('"', '\\"')


def guess_extension(url: str) -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext and len(ext) <= 5 and ext[1:].isalnum():
        return ext
    return ".jpg"


def build_image_candidates(src: str) -> list:
    """
    원본 화질 이미지 URL 후보를 우선순위대로 만든다.

    본문 img src는 ?type=w386 같은 축소 썸네일이고, 쿼리를 그냥 지우면
    3KB짜리 placeholder가 내려온다. 실제 원본은 blogfiles.pstatic.net(쿼리 없음)
    또는 postfiles + ?type=w3840 이다. (w3840은 원본보다 확대하지 않는다)
    """
    base = src.split("?")[0]
    candidates = []
    if "postfiles" in base:
        candidates.append(base.replace("postfiles", "blogfiles"))
    candidates.append(f"{base}?type=w3840")
    candidates.append(f"{base}?type=w966")
    candidates.append(base)
    return candidates


def _download_one_image(session, src, filepath):
    for candidate in build_image_candidates(src):
        try:
            resp = session.get(candidate, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200 or not resp.content:
                continue
            filepath.write_bytes(resp.content)
            return True
        except Exception:
            continue
    return False


def download_images_and_rewrite(session, soup, logno: str, attachments_dir: Path, enabled: bool):
    img_tags = soup.find_all("img")
    if not img_tags:
        return 0

    if not enabled:
        for img in img_tags:
            img.decompose()
        return 0

    attachments_dir.mkdir(parents=True, exist_ok=True)
    jobs = []

    for idx, img in enumerate(img_tags, start=1):
        src = (img.get("data-lazy-src") or img.get("src")
               or img.get("data-src") or img.get("origin-src"))
        if not src or src.startswith("data:"):
            img.decompose()
            continue
        if src.startswith("//"):
            src = "https:" + src

        clean_url = src.split("?")[0]
        filename = sanitize_filename(f"{logno}_{idx}{guess_extension(clean_url)}")
        jobs.append((img, clean_url, attachments_dir / filename, filename))

    if not jobs:
        return 0

    with ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as pool:
        results = list(pool.map(lambda j: _download_one_image(session, j[1], j[2]), jobs))

    saved = 0
    for (img, _, _, filename), ok in zip(jobs, results):
        if not ok:
            img.decompose()
            continue
        for attr in list(img.attrs):
            del img[attr]
        img["src"] = filename
        saved += 1
    return saved


def html_to_markdown(container) -> str:
    # 네이버는 이미지를 <a href="#">로 감싸므로, 그대로 두면 [![[img]]](#) 형태가 된다.
    for link in container.find_all("a"):
        if link.find("img") and not (link.get("href") or "").startswith("http"):
            link.unwrap()

    text = md(str(container), heading_style="ATX", bullets="-")
    text = re.sub(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", r"![[\1]]", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


LINK_SECTION_HEADERS = ("## 카테고리", "## 관련 글", "## 비슷한 글")


def strip_linked_sections(body: str) -> str:
    for header in LINK_SECTION_HEADERS:
        body = re.sub(rf"\n+{re.escape(header)}\n.*?(?=\n## |\Z)", "\n", body, flags=re.S)
    return body.rstrip()


def strip_markdown_for_similarity(markdown_text: str) -> str:
    text = re.sub(r"!\[\[.*?\]\]", " ", markdown_text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[\[([^|\]]*\|)?([^\]]*)\]\]", r"\2", text)
    text = re.sub(r"[#*`_>-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:TEXT_SAMPLE_LENGTH]


def save_markdown(out_dir: Path, logno: str, title: str, date: str, category: str,
                  tags: list, url: str, body: str) -> Path:
    filepath = out_dir / f"{logno}_{sanitize_filename(title) or logno}.md"
    tags_yaml = ", ".join(f'"{yaml_escape(t)}"' for t in tags)
    frontmatter = (
        "---\n"
        f'title: "{yaml_escape(title)}"\n'
        f"date: {date or '0000-00-00'}\n"
        f'category: "{yaml_escape(category)}"\n'
        f"tags: [{tags_yaml}]\n"
        f"source: {url}\n"
        "---\n\n"
    )
    filepath.write_text(frontmatter + body + "\n", encoding="utf-8")
    return filepath


# ============================== 자동 링크 ==============================
def create_category_hub_notes(out_dir: Path, posts_index: dict, log):
    by_category = {}
    for meta in posts_index.values():
        by_category.setdefault(meta.get("category") or "미분류", []).append(meta)

    for cat, items in by_category.items():
        items.sort(key=lambda m: m["filename"])
        hub_path = out_dir / f"카테고리 - {sanitize_filename(cat) or '미분류'}.md"
        links_md = "\n".join(f"- [[{m['filename']}|{m['title']}]]" for m in items)
        hub_path.write_text(
            "---\n"
            f'title: "카테고리 - {yaml_escape(cat)}"\n'
            "type: category-index\n"
            "---\n\n"
            f"# {cat} ({len(items)}개 글)\n\n{links_md}\n",
            encoding="utf-8",
        )

        for meta in items:
            post_path = out_dir / f"{meta['filename']}.md"
            if not post_path.exists():
                continue
            content = post_path.read_text(encoding="utf-8")
            content = re.sub(r"\n+## 카테고리\n.*?(?=\n## |\Z)", "\n", content, flags=re.S).rstrip()
            content += f"\n\n## 카테고리\n- [[{hub_path.stem}|{cat}]]\n"
            post_path.write_text(content, encoding="utf-8")

    log(f"카테고리 허브 노트 {len(by_category)}개 생성")


def apply_related_links(out_dir: Path, posts_index: dict, log):
    entries = list(posts_index.items())
    linked = 0

    for logno, meta in entries:
        my_tags = set(meta.get("tags") or [])
        if not my_tags:
            continue

        scored = []
        for other_logno, other in entries:
            if other_logno == logno:
                continue
            shared = my_tags & set(other.get("tags") or [])
            if len(shared) >= MIN_SHARED_TAGS:
                scored.append((len(shared), other))
        if not scored:
            continue

        scored.sort(key=lambda x: -x[0])
        filepath = out_dir / f"{meta['filename']}.md"
        if not filepath.exists():
            continue

        content = filepath.read_text(encoding="utf-8")
        content = re.sub(r"\n+## 관련 글\n.*?(?=\n## |\Z)", "\n", content, flags=re.S).rstrip()
        links_md = "\n".join(f"- [[{o['filename']}|{o['title']}]]"
                             for _, o in scored[:RELATED_MAX_LINKS])
        content += f"\n\n## 관련 글\n{links_md}\n"
        filepath.write_text(content, encoding="utf-8")
        linked += 1

    if linked:
        log(f"태그 기반 관련 글 링크 {linked}개 글에 추가")


def _char_ngrams(text: str) -> Counter:
    text = re.sub(r"\s+", " ", text)
    counter = Counter()
    for size in NGRAM_SIZES:
        for i in range(len(text) - size + 1):
            counter[text[i:i + size]] += 1
    return counter


def _build_tfidf_vectors(docs: dict) -> dict:
    ngram_docs = {k: _char_ngrams(v) for k, v in docs.items()}
    n_docs = len(docs) or 1

    df = Counter()
    for grams in ngram_docs.values():
        for g in grams:
            df[g] += 1
    max_df = max(1, int(n_docs * DF_MAX_RATIO))

    vectors = {}
    for k, grams in ngram_docs.items():
        total = sum(grams.values()) or 1
        vec = {}
        for g, count in grams.items():
            if df[g] > max_df:
                continue
            vec[g] = (count / total) * (math.log((n_docs + 1) / (df[g] + 1)) + 1)
        vectors[k] = vec
    return vectors


def _cosine_similarity(vec_a: dict, vec_b: dict) -> float:
    if len(vec_a) > len(vec_b):
        vec_a, vec_b = vec_b, vec_a
    dot = sum(w * vec_b.get(g, 0.0) for g, w in vec_a.items())
    norm_a = math.sqrt(sum(w * w for w in vec_a.values()))
    norm_b = math.sqrt(sum(w * w for w in vec_b.values()))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def apply_similar_content_links(out_dir: Path, posts_index: dict, log):
    entries = [(k, m) for k, m in posts_index.items() if m.get("text_sample")]
    if len(entries) < 2:
        return

    log(f"{len(entries)}개 글의 본문 유사도를 계산합니다...")
    docs = {k: f"{m['title']} {m['text_sample']}" for k, m in entries}
    vectors = _build_tfidf_vectors(docs)

    inverted = {}
    for key, vec in vectors.items():
        for g in vec:
            inverted.setdefault(g, []).append(key)

    meta_by_logno = dict(entries)
    linked = 0

    for logno, meta in entries:
        vec = vectors[logno]
        if not vec:
            continue

        candidates = set()
        for g in vec:
            candidates.update(inverted.get(g, ()))
        candidates.discard(logno)

        scored = [(s, o) for o in candidates
                  if (s := _cosine_similarity(vec, vectors[o])) >= SIMILARITY_THRESHOLD]
        if not scored:
            continue

        scored.sort(key=lambda x: -x[0])
        filepath = out_dir / f"{meta['filename']}.md"
        if not filepath.exists():
            continue

        content = filepath.read_text(encoding="utf-8")
        content = re.sub(r"\n+## 비슷한 글\n.*?(?=\n## |\Z)", "\n", content, flags=re.S).rstrip()
        links_md = "\n".join(
            f"- [[{meta_by_logno[o]['filename']}|{meta_by_logno[o]['title']}]] (유사도 {s:.2f})"
            for s, o in scored[:SIMILAR_MAX_LINKS]
        )
        content += f"\n\n## 비슷한 글\n{links_md}\n"
        filepath.write_text(content, encoding="utf-8")
        linked += 1

    log(f"본문 유사도 링크 {linked}개 글에 추가")


def rebuild_index_from_existing_files(out_dir: Path) -> dict:
    """이전 실행으로 이미 저장된 .md 파일도 링크 계산에 포함시킨다."""
    posts_index = {}
    if not out_dir.exists():
        return posts_index

    for filepath in out_dir.glob("*.md"):
        m = re.match(r"(\d+)_", filepath.name)
        if not m:
            continue
        try:
            text = filepath.read_text(encoding="utf-8")
        except OSError:
            continue

        title_m = re.search(r'^title:\s*"(.*)"\s*$', text, re.MULTILINE)
        tags_m = re.search(r"^tags:\s*\[(.*)\]\s*$", text, re.MULTILINE)
        category_m = re.search(r'^category:\s*"(.*)"\s*$', text, re.MULTILINE)

        parts = text.split("---\n", 2)
        body = parts[2].lstrip("\n") if len(parts) >= 3 else ""

        posts_index[m.group(1)] = {
            "filename": filepath.stem,
            "title": title_m.group(1).replace('\\"', '"') if title_m else filepath.stem,
            "tags": re.findall(r'"([^"]*)"', tags_m.group(1)) if tags_m else [],
            "category": category_m.group(1).replace('\\"', '"') if category_m else "미분류",
            "text_sample": strip_markdown_for_similarity(strip_linked_sections(body)),
        }
    return posts_index


# ============================== 실행 ==============================
def run_migration(settings: dict, log, progress, should_stop):
    out_dir = Path(settings["out_dir"])
    attachments_dir = out_dir / "attachments"
    out_dir.mkdir(parents=True, exist_ok=True)

    session, blog_id = login_and_get_session(log, should_stop)

    category_names = fetch_category_names(session, blog_id, log)
    post_list = collect_posts(session, blog_id, settings["max_posts"], log, should_stop)
    if not post_list:
        raise RuntimeError("가져올 글이 없습니다.")

    log("해시태그를 가져옵니다...")
    tag_map = fetch_tags(session, blog_id, [p["logNo"] for p in post_list])

    posts_index = rebuild_index_from_existing_files(out_dir)
    failed = []
    total = len(post_list)
    started = time.time()
    image_count = 0

    log(f"총 {total}개의 글을 저장합니다 -> {out_dir}")
    progress(0, total, "")

    for i, entry in enumerate(post_list, start=1):
        if should_stop():
            raise Stopped()

        logno = entry["logNo"]

        if any(out_dir.glob(f"{logno}_*.md")):
            log(f"[{i}/{total}] 이미 있음 - 건너뜀: {entry['title']}")
            progress(i, total, entry["title"])
            continue

        try:
            html = fetch_post_html(session, blog_id, logno)
            parsed = parse_post(html)

            title = parsed["title"] or entry["title"]
            date = parsed["date"] or entry["date"]
            category = category_names.get(entry["category_no"], "미분류")
            tags = tag_map.get(logno) or parsed["tags"]

            image_count += download_images_and_rewrite(
                session, parsed["container"], logno, attachments_dir, settings["download_images"])
            body = html_to_markdown(parsed["container"])

            filepath = save_markdown(out_dir, logno, title, date, category, tags,
                                     entry["url"], body)

            posts_index[logno] = {
                "filename": filepath.stem,
                "title": title,
                "tags": tags,
                "category": category,
                "text_sample": strip_markdown_for_similarity(body),
            }

            elapsed = time.time() - started
            eta = elapsed / i * (total - i)
            log(f"[{i}/{total}] {title}  ({category})  · 남은 시간 약 {eta/60:.1f}분")
        except Stopped:
            raise
        except Exception as e:
            log(f"[{i}/{total}] [에러] {entry['url']} -> {e}")
            failed.append(entry["url"])

        progress(i, total, entry["title"])

    if settings["make_links"] and posts_index:
        log("")
        create_category_hub_notes(out_dir, posts_index, log)
        apply_related_links(out_dir, posts_index, log)
        apply_similar_content_links(out_dir, posts_index, log)

    if failed:
        (out_dir / "_failed_urls.txt").write_text("\n".join(failed), encoding="utf-8")
        log(f"\n실패한 글 {len(failed)}개 (_failed_urls.txt 에 저장). 다시 실행하면 재시도합니다.")

    return {
        "elapsed": time.time() - started,
        "saved": total - len(failed),
        "failed": len(failed),
        "images": image_count,
        "out_dir": out_dir,
    }


# ============================== GUI ==============================
BG = "#f7f7f9"
ACCENT = "#03c75a"        # 네이버 그린
ACCENT_DARK = "#02a047"
TEXT = "#1a1a1a"
MUTED = "#6b7280"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("네이버 블로그 → 옵시디언")
        self.geometry("720x760")
        self.minsize(640, 700)
        self.configure(bg=BG)

        self.msg_queue = queue.Queue()
        self.worker = None
        self.stop_flag = threading.Event()

        self._build_ui()
        self._load_settings()
        self.after(100, self._drain_queue)

    # ---------- UI 구성 ----------
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TProgressbar", troughcolor="#e5e7eb", background=ACCENT,
                        bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT, thickness=14)

        header = tk.Frame(self, bg=ACCENT, height=76)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="네이버 블로그 → 옵시디언", bg=ACCENT, fg="white",
                 font=("맑은 고딕", 16, "bold")).pack(anchor="w", padx=24, pady=(16, 0))
        tk.Label(header, text="글·이미지를 옵시디언 노트로 옮기고 자동으로 연결합니다",
                 bg=ACCENT, fg="#e8fff1", font=("맑은 고딕", 9)).pack(anchor="w", padx=24)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=24, pady=18)

        # 저장 위치
        tk.Label(body, text="저장 위치", bg=BG, fg=TEXT,
                 font=("맑은 고딕", 10, "bold")).pack(anchor="w")
        path_row = tk.Frame(body, bg=BG)
        path_row.pack(fill="x", pady=(6, 2))

        self.path_var = tk.StringVar(value=str(default_output_dir()))
        entry = tk.Entry(path_row, textvariable=self.path_var, font=("맑은 고딕", 9),
                         relief="solid", bd=1, bg="white")
        entry.pack(side="left", fill="x", expand=True, ipady=6)
        tk.Button(path_row, text="폴더 선택", command=self._choose_folder,
                  bg="white", fg=TEXT, relief="solid", bd=1, cursor="hand2",
                  font=("맑은 고딕", 9), padx=14).pack(side="left", padx=(8, 0))

        self.path_hint = tk.Label(body, text="", bg=BG, fg=MUTED, font=("맑은 고딕", 8))
        self.path_hint.pack(anchor="w")
        self._update_hint()
        self.path_var.trace_add("write", lambda *_: self._update_hint())

        # 가져올 글
        tk.Label(body, text="가져올 글", bg=BG, fg=TEXT,
                 font=("맑은 고딕", 10, "bold")).pack(anchor="w", pady=(16, 0))
        count_row = tk.Frame(body, bg=BG)
        count_row.pack(fill="x", pady=(6, 0))

        self.scope_var = tk.StringVar(value="all")
        tk.Radiobutton(count_row, text="전체", variable=self.scope_var, value="all",
                       bg=BG, fg=TEXT, activebackground=BG, font=("맑은 고딕", 9),
                       cursor="hand2", command=self._toggle_count).pack(side="left")
        tk.Radiobutton(count_row, text="최신", variable=self.scope_var, value="some",
                       bg=BG, fg=TEXT, activebackground=BG, font=("맑은 고딕", 9),
                       cursor="hand2", command=self._toggle_count).pack(side="left", padx=(14, 4))
        self.count_spin = tk.Spinbox(count_row, from_=1, to=9999, width=6, font=("맑은 고딕", 9),
                                     relief="solid", bd=1, justify="center")
        self.count_spin.delete(0, "end")
        self.count_spin.insert(0, "10")
        self.count_spin.pack(side="left")
        tk.Label(count_row, text="개", bg=BG, fg=TEXT, font=("맑은 고딕", 9)).pack(side="left", padx=(4, 0))
        self._toggle_count()

        # 옵션
        tk.Label(body, text="옵션", bg=BG, fg=TEXT,
                 font=("맑은 고딕", 10, "bold")).pack(anchor="w", pady=(16, 0))
        self.img_var = tk.BooleanVar(value=True)
        self.link_var = tk.BooleanVar(value=True)
        tk.Checkbutton(body, text="이미지를 원본 화질로 저장", variable=self.img_var,
                       bg=BG, fg=TEXT, activebackground=BG, font=("맑은 고딕", 9),
                       cursor="hand2").pack(anchor="w", pady=(4, 0))
        tk.Checkbutton(body, text="카테고리 · 관련 글 자동 링크 만들기", variable=self.link_var,
                       bg=BG, fg=TEXT, activebackground=BG, font=("맑은 고딕", 9),
                       cursor="hand2").pack(anchor="w")

        # 시작 버튼
        self.start_btn = tk.Button(body, text="시작하기", command=self._start,
                                   bg=ACCENT, fg="white", relief="flat", cursor="hand2",
                                   font=("맑은 고딕", 11, "bold"), pady=10,
                                   activebackground=ACCENT_DARK, activeforeground="white")
        self.start_btn.pack(fill="x", pady=(18, 10))

        # 진행 상황
        self.progress_label = tk.Label(body, text="대기 중", bg=BG, fg=MUTED,
                                       font=("맑은 고딕", 9), anchor="w")
        self.progress_label.pack(fill="x")
        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(4, 10))

        log_frame = tk.Frame(body, bg=BG)
        log_frame.pack(fill="both", expand=True)
        scroll = tk.Scrollbar(log_frame)
        scroll.pack(side="right", fill="y")
        self.log_box = tk.Text(log_frame, height=8, font=("Consolas", 8), relief="solid", bd=1,
                               bg="white", fg="#374151", yscrollcommand=scroll.set, wrap="word")
        self.log_box.pack(fill="both", expand=True)
        scroll.config(command=self.log_box.yview)
        self.log_box.configure(state="disabled")

    def _update_hint(self):
        path = Path(self.path_var.get())
        if (path / ".obsidian").exists() or (path.parent / ".obsidian").exists():
            self.path_hint.config(text="옵시디언 볼트 안입니다. 옵시디언에서 바로 보입니다.", fg="#059669")
        else:
            self.path_hint.config(
                text="옵시디언 볼트 밖입니다. 볼트 안 폴더를 고르면 옵시디언에서 바로 볼 수 있습니다.",
                fg="#d97706")

    def _toggle_count(self):
        self.count_spin.config(state="normal" if self.scope_var.get() == "some" else "disabled")

    def _choose_folder(self):
        current = Path(self.path_var.get())
        initial = current if current.exists() else (current.parent if current.parent.exists() else Path.home())
        chosen = filedialog.askdirectory(title="글을 저장할 폴더를 선택하세요", initialdir=str(initial))
        if chosen:
            self.path_var.set(str(Path(chosen)))

    # ---------- 설정 저장/불러오기 ----------
    def _load_settings(self):
        if not SETTINGS_FILE.exists():
            return
        try:
            s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        if s.get("out_dir"):
            self.path_var.set(s["out_dir"])
        self.img_var.set(s.get("download_images", True))
        self.link_var.set(s.get("make_links", True))
        if s.get("max_posts"):
            self.scope_var.set("some")
            self.count_spin.delete(0, "end")
            self.count_spin.insert(0, str(s["max_posts"]))
        self._toggle_count()
        self._update_hint()

    def _save_settings(self, settings):
        try:
            SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        except OSError:
            pass

    # ---------- 실행 ----------
    def _start(self):
        if self.worker and self.worker.is_alive():
            self.stop_flag.set()
            self.start_btn.config(text="중단하는 중...", state="disabled")
            return

        max_posts = 0
        if self.scope_var.get() == "some":
            try:
                max_posts = max(1, int(self.count_spin.get()))
            except ValueError:
                messagebox.showwarning("확인", "가져올 글 개수를 숫자로 입력해 주세요.")
                return

        settings = {
            "out_dir": self.path_var.get().strip(),
            "max_posts": max_posts,
            "download_images": self.img_var.get(),
            "make_links": self.link_var.get(),
        }
        if not settings["out_dir"]:
            messagebox.showwarning("확인", "저장 위치를 선택해 주세요.")
            return

        self._save_settings(settings)

        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.progress["value"] = 0
        self.stop_flag.clear()
        self.start_btn.config(text="중단하기", bg="#ef4444", activebackground="#dc2626")

        self.worker = threading.Thread(target=self._work, args=(settings,), daemon=True)
        self.worker.start()

    def _work(self, settings):
        def log(msg):
            self.msg_queue.put(("log", msg))

        def progress(current, total, title):
            self.msg_queue.put(("progress", (current, total, title)))

        try:
            result = run_migration(settings, log, progress, self.stop_flag.is_set)
            self.msg_queue.put(("done", result))
        except Stopped:
            self.msg_queue.put(("stopped", None))
        except Exception as e:
            self.msg_queue.put(("error", f"{e}\n\n{traceback.format_exc()}"))

    # ---------- 메시지 처리 ----------
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()

                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", str(payload) + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")

                elif kind == "progress":
                    current, total, title = payload
                    self.progress["value"] = (current / total * 100) if total else 0
                    self.progress_label.config(
                        text=f"{current} / {total}  ·  {title[:40]}" if total else "대기 중")

                elif kind == "done":
                    self._finish()
                    r = payload
                    self.progress["value"] = 100
                    self.progress_label.config(text=f"완료 · {r['saved']}개 저장", fg="#059669")
                    messagebox.showinfo(
                        "완료",
                        f"글 {r['saved']}개를 저장했습니다.\n"
                        f"이미지 {r['images']}장 · 소요 시간 {r['elapsed']/60:.1f}분\n"
                        f"실패 {r['failed']}개\n\n{r['out_dir']}",
                    )

                elif kind == "stopped":
                    self._finish()
                    self.progress_label.config(text="중단됨", fg="#d97706")

                elif kind == "error":
                    self._finish()
                    self.progress_label.config(text="오류 발생", fg="#dc2626")
                    messagebox.showerror("오류", str(payload)[:1200])
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _finish(self):
        self.start_btn.config(text="시작하기", state="normal", bg=ACCENT,
                              activebackground=ACCENT_DARK)


if __name__ == "__main__":
    App().mainloop()
