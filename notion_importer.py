"""
노션(Notion) -> 옵시디언(Obsidian) 마이그레이션
==============================================

- 페이지, 데이터베이스, 하위 페이지 계층 구조를 그대로 폴더로 옮깁니다.
- 이미지 · 파일 · PDF 등 첨부는 전부 내려받아 attachments 폴더에 넣습니다.
- 데이터베이스 속성(태그, 날짜, 선택 등)은 YAML Frontmatter 로 들어갑니다.
- 페이지끼리의 링크는 옵시디언 위키링크([[...]])로 바뀝니다.

읽기 전용입니다. 노션의 데이터는 절대 수정하거나 지우지 않습니다.
(사용하는 API 는 GET /search /query 뿐이며, 쓰기·삭제 호출은 하지 않습니다.)
"""

import json
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests

from migrator_common import Stopped

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

PAGE_SIZE = 100
REQUEST_TIMEOUT = 30
MIN_INTERVAL = 0.34          # 노션 제한: 초당 약 3회
MAX_RETRIES = 4
FILE_WORKERS = 8

# 내려받을 첨부 블록
FILE_BLOCKS = {"image", "file", "pdf", "video", "audio"}

# 블록 안에서 페이지 링크를 잠시 표시해 둘 자리표시자
PAGE_REF = "\x00PAGEREF:{}\x00"
PAGE_REF_RE = re.compile("\x00PAGEREF:([0-9a-f-]+)\x00")

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


# ============================== API ==============================
class NotionClient:
    """노션 API 호출기. 속도 제한을 지키며 읽기만 한다."""

    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self._lock = threading.Lock()
        self._last_call = 0.0
        self.calls = 0

    def _throttle(self):
        with self._lock:
            wait = MIN_INTERVAL - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            self.calls += 1

    def request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{NOTION_API}{path}"
        for attempt in range(MAX_RETRIES):
            self._throttle()
            try:
                resp = self.session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            except requests.RequestException:
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 2)))
                continue
            if resp.status_code >= 500:
                if attempt == MAX_RETRIES - 1:
                    resp.raise_for_status()
                time.sleep(2 ** attempt)
                continue
            if resp.status_code != 200:
                raise RuntimeError(_api_error(resp))
            return resp.json()
        raise RuntimeError("노션 API 호출에 계속 실패했습니다.")

    def paginate(self, method: str, path: str, body: dict = None) -> list:
        """커서를 따라가며 전부 모은다."""
        results, cursor = [], None
        while True:
            if method == "GET":
                params = {"page_size": PAGE_SIZE}
                if cursor:
                    params["start_cursor"] = cursor
                data = self.request("GET", path, params=params)
            else:
                payload = dict(body or {})
                payload["page_size"] = PAGE_SIZE
                if cursor:
                    payload["start_cursor"] = cursor
                data = self.request("POST", path, json=payload)

            results.extend(data.get("results", []))
            if not data.get("has_more"):
                return results
            cursor = data.get("next_cursor")

    # --- 개별 엔드포인트 ---
    def whoami(self) -> dict:
        return self.request("GET", "/users/me")

    def search(self) -> list:
        return self.paginate("POST", "/search", {})

    def page(self, page_id: str) -> dict:
        return self.request("GET", f"/pages/{page_id}")

    def database(self, db_id: str) -> dict:
        return self.request("GET", f"/databases/{db_id}")

    def children(self, block_id: str) -> list:
        return self.paginate("GET", f"/blocks/{block_id}/children")

    def query_database(self, db_id: str) -> list:
        return self.paginate("POST", f"/databases/{db_id}/query", {})


def _api_error(resp) -> str:
    try:
        data = resp.json()
        code = data.get("code", "")
        msg = data.get("message", resp.text[:200])
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"

    if code == "unauthorized":
        return "노션 토큰이 올바르지 않습니다. 통합(Integration) 토큰을 다시 확인해 주세요."
    if code == "restricted_resource":
        return "이 페이지에 접근 권한이 없습니다. 노션에서 통합을 연결해 주세요."
    if code == "object_not_found":
        return "페이지를 찾을 수 없습니다. 노션에서 통합을 연결했는지 확인해 주세요."
    return f"{code}: {msg}"


# ============================== 값 변환 ==============================
def sanitize_filename(name: str, fallback: str = "제목없음") -> str:
    name = INVALID_CHARS.sub(" ", name or "")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if not name:
        return fallback
    return name[:110]


def yaml_escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _wrap(text: str, mark: str) -> str:
    """양쪽 공백은 밖으로 빼고 강조 기호를 씌운다 (**굵게 ** 는 깨지므로)."""
    stripped = text.strip()
    if not stripped:
        return text
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return f"{lead}{mark}{stripped}{mark}{trail}"


def rich_text_to_md(rich_text: list) -> str:
    out = []
    for rt in rich_text or []:
        kind = rt.get("type")

        if kind == "equation":
            expr = (rt.get("equation") or {}).get("expression", "")
            out.append(f"${expr}$")
            continue

        if kind == "mention":
            mention = rt.get("mention") or {}
            mtype = mention.get("type")
            if mtype in ("page", "database"):
                ref_id = (mention.get(mtype) or {}).get("id")
                if ref_id:
                    out.append(PAGE_REF.format(ref_id))
                    continue
            out.append(rt.get("plain_text", ""))
            continue

        text = rt.get("plain_text", "")
        if not text:
            continue

        ann = rt.get("annotations") or {}
        if ann.get("code"):
            text = f"`{text}`"
        else:
            if ann.get("bold"):
                text = _wrap(text, "**")
            if ann.get("italic"):
                text = _wrap(text, "*")
            if ann.get("strikethrough"):
                text = _wrap(text, "~~")
            if ann.get("underline"):
                text = f"<u>{text}</u>"

        href = rt.get("href")
        if href:
            text = f"[{text}]({href})"
        out.append(text)

    return "".join(out)


def plain_text(rich_text: list) -> str:
    return "".join(rt.get("plain_text", "") for rt in rich_text or [])


def property_to_value(prop: dict):
    """노션 속성 하나를 Frontmatter 에 넣을 값으로 바꾼다."""
    kind = prop.get("type")
    val = prop.get(kind)

    if kind in ("title", "rich_text"):
        return plain_text(val)
    if kind == "number":
        return val
    if kind == "checkbox":
        return bool(val)
    if kind in ("url", "email", "phone_number"):
        return val
    if kind in ("select", "status"):
        return (val or {}).get("name")
    if kind == "multi_select":
        return [o.get("name") for o in val or []]
    if kind == "date":
        if not val:
            return None
        start, end = val.get("start"), val.get("end")
        return f"{start} ~ {end}" if end else start
    if kind in ("created_time", "last_edited_time"):
        return val
    if kind in ("created_by", "last_edited_by"):
        return (val or {}).get("name")
    if kind == "people":
        return [p.get("name") for p in val or [] if p.get("name")]
    if kind == "files":
        names = []
        for f in val or []:
            names.append(f.get("name") or _file_url(f) or "")
        return [n for n in names if n]
    if kind == "relation":
        return [PAGE_REF.format(r["id"]) for r in val or [] if r.get("id")]
    if kind == "unique_id":
        prefix = (val or {}).get("prefix") or ""
        return f"{prefix}{(val or {}).get('number', '')}"
    if kind == "formula":
        inner = val or {}
        return property_to_value({"type": inner.get("type"), inner.get("type"): inner.get(inner.get("type"))})
    if kind == "rollup":
        inner = val or {}
        if inner.get("type") == "array":
            return [property_to_value(x) for x in inner.get("array", [])]
        return property_to_value({"type": inner.get("type"), inner.get("type"): inner.get(inner.get("type"))})
    if kind == "verification":
        return (val or {}).get("state")
    return None


def _file_url(obj: dict) -> str:
    """노션 파일 객체에서 실제 URL 을 꺼낸다 (업로드 파일 / 외부 링크 둘 다)."""
    if not obj:
        return ""
    kind = obj.get("type")
    if kind == "file":
        return (obj.get("file") or {}).get("url", "")
    if kind == "external":
        return (obj.get("external") or {}).get("url", "")
    return (obj.get("file") or {}).get("url", "") or (obj.get("external") or {}).get("url", "")


def page_title(page: dict) -> str:
    """페이지(또는 DB 행)의 제목."""
    if page.get("object") == "database":
        return plain_text(page.get("title")) or "제목없는 데이터베이스"
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            title = plain_text(prop.get("title"))
            if title:
                return title
    return "제목없음"


# ============================== 첨부 파일 ==============================
def _extension_from_url(url: str, default: str = ".bin") -> str:
    path = unquote(urlparse(url).path)
    suffix = Path(path).suffix
    if suffix and len(suffix) <= 6:
        return suffix
    return default


def queue_download(ctx: dict, url: str, hint: str) -> str:
    """첨부를 내려받기 목록에 넣고, 노트에 쓸 파일 이름을 돌려준다."""
    if not url:
        return ""
    base = sanitize_filename(Path(unquote(urlparse(url).path)).stem or hint, hint)
    ext = _extension_from_url(url, ".png" if "image" in hint else ".bin")
    name = f"{base}{ext}"

    seen = ctx["file_names"]
    if name in seen and seen[name] != url:
        n = 2
        while f"{base}_{n}{ext}" in seen and seen[f"{base}_{n}{ext}"] != url:
            n += 1
        name = f"{base}_{n}{ext}"
    seen[name] = url

    ctx["downloads"].append((url, name))
    return name


def download_files(session: requests.Session, downloads: list, attachments_dir: Path, log) -> int:
    """S3 서명 URL 은 한 시간이면 만료되므로 페이지를 만들자마자 바로 받는다."""
    if not downloads:
        return 0
    attachments_dir.mkdir(parents=True, exist_ok=True)

    def fetch(item):
        url, name = item
        target = attachments_dir / name
        if target.exists() and target.stat().st_size > 0:
            return True
        try:
            with session.get(url, timeout=REQUEST_TIMEOUT, stream=True) as r:
                r.raise_for_status()
                with open(target, "wb") as fp:
                    for chunk in r.iter_content(65536):
                        fp.write(chunk)
            return True
        except (requests.RequestException, OSError):
            target.unlink(missing_ok=True)
            return False

    with ThreadPoolExecutor(max_workers=FILE_WORKERS) as pool:
        results = list(pool.map(fetch, downloads))

    failed = results.count(False)
    if failed:
        log(f"    첨부 {failed}개를 받지 못했습니다.")
    return results.count(True)


# ============================== 블록 -> 마크다운 ==============================
CALLOUT_ICON_DEFAULT = "note"

HEADING_PREFIX = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}


def blocks_to_markdown(client: NotionClient, blocks: list, ctx: dict,
                       should_stop, depth: int = 0) -> str:
    """블록 목록을 마크다운 문자열로. 하위 블록은 재귀로 들어간다."""
    if depth > 12:
        return ""

    lines = []
    number = 0
    prev_type = None

    for block in blocks:
        if should_stop():
            raise Stopped()

        btype = block.get("type", "")
        data = block.get(btype) or {}

        if btype == "numbered_list_item":
            number = number + 1 if prev_type == "numbered_list_item" else 1
        else:
            number = 0

        rendered = _render_block(client, block, btype, data, ctx, should_stop, depth, number)
        if rendered is not None:
            lines.append(rendered)

        prev_type = btype

    return "\n\n".join(x for x in lines if x is not None)


def _children_md(client, block, ctx, should_stop, depth, indent="") -> str:
    if not block.get("has_children"):
        return ""
    kids = client.children(block["id"])
    body = blocks_to_markdown(client, kids, ctx, should_stop, depth + 1)
    if not body or not indent:
        return body
    return "\n".join(indent + line if line else line for line in body.split("\n"))


def _render_block(client, block, btype, data, ctx, should_stop, depth, number):
    text = rich_text_to_md(data.get("rich_text"))

    if btype == "paragraph":
        body = _children_md(client, block, ctx, should_stop, depth, "    ")
        return "\n\n".join(p for p in (text, body) if p) or None

    if btype in HEADING_PREFIX:
        head = f"{HEADING_PREFIX[btype]} {text}".rstrip()
        body = _children_md(client, block, ctx, should_stop, depth)
        return "\n\n".join(p for p in (head, body) if p)

    if btype in ("bulleted_list_item", "numbered_list_item", "to_do"):
        if btype == "to_do":
            marker = "- [x]" if data.get("checked") else "- [ ]"
        elif btype == "numbered_list_item":
            marker = f"{number}."
        else:
            marker = "-"
        body = _children_md(client, block, ctx, should_stop, depth, "    ")
        item = f"{marker} {text}".rstrip()
        return f"{item}\n\n{body}" if body else item

    if btype == "toggle":
        body = _children_md(client, block, ctx, should_stop, depth)
        head = f"> [!note]- {text}".rstrip()
        if body:
            head += "\n" + "\n".join(f"> {line}" if line else ">" for line in body.split("\n"))
        return head

    if btype == "quote":
        body = _children_md(client, block, ctx, should_stop, depth)
        full = "\n\n".join(p for p in (text, body) if p)
        return "\n".join(f"> {line}" if line else ">" for line in full.split("\n"))

    if btype == "callout":
        icon = (data.get("icon") or {}).get("emoji", "")
        body = _children_md(client, block, ctx, should_stop, depth)
        full = "\n\n".join(p for p in (text, body) if p)
        head = f"> [!{CALLOUT_ICON_DEFAULT}] {icon}".rstrip()
        return head + "\n" + "\n".join(f"> {line}" if line else ">" for line in full.split("\n"))

    if btype == "code":
        lang = (data.get("language") or "").replace(" ", "-")
        code = plain_text(data.get("rich_text"))
        caption = rich_text_to_md(data.get("caption"))
        out = f"```{lang}\n{code}\n```"
        return f"{out}\n*{caption}*" if caption else out

    if btype == "equation":
        return f"$$\n{data.get('expression', '')}\n$$"

    if btype == "divider":
        return "---"

    if btype in FILE_BLOCKS:
        return _render_file(block, btype, data, ctx)

    if btype in ("bookmark", "embed", "link_preview"):
        url = data.get("url", "")
        caption = rich_text_to_md(data.get("caption")) or url
        return f"[{caption}]({url})" if url else None

    if btype == "table":
        return _render_table(client, block, data, ctx, should_stop)

    if btype == "column_list":
        return _children_md(client, block, ctx, should_stop, depth)

    if btype == "column":
        return _children_md(client, block, ctx, should_stop, depth)

    if btype == "synced_block":
        source = (data.get("synced_from") or {}).get("block_id")
        if source:
            try:
                kids = client.children(source)
                return blocks_to_markdown(client, kids, ctx, should_stop, depth + 1)
            except RuntimeError:
                return None
        return _children_md(client, block, ctx, should_stop, depth)

    if btype in ("child_page", "child_database"):
        ctx["discovered"].append(block["id"])
        return PAGE_REF.format(block["id"])

    if btype == "link_to_page":
        ref = data.get("page_id") or data.get("database_id")
        if ref:
            ctx["discovered"].append(ref)
            return PAGE_REF.format(ref)
        return None

    if btype == "table_of_contents":
        return None
    if btype == "breadcrumb":
        return None
    if btype == "template":
        return None

    if text:
        return text
    return None


def _render_file(block, btype, data, ctx):
    url = _file_url(data)
    if not url:
        return None
    caption = rich_text_to_md(data.get("caption"))

    external = data.get("type") == "external"
    if external and not ctx["download_external"]:
        return f"![{caption}]({url})" if btype == "image" else f"[{caption or url}]({url})"

    name = queue_download(ctx, url, btype)
    if not name:
        return None

    embed = f"![[{name}]]"
    return f"{embed}\n*{caption}*" if caption else embed


def _render_table(client, block, data, ctx, should_stop):
    if not block.get("has_children"):
        return None
    rows = client.children(block["id"])
    cells = [
        [rich_text_to_md(c).replace("|", "\\|").replace("\n", " ") for c in (r.get("table_row") or {}).get("cells", [])]
        for r in rows if r.get("type") == "table_row"
    ]
    if not cells:
        return None

    width = max(len(r) for r in cells)
    cells = [r + [""] * (width - len(r)) for r in cells]

    if data.get("has_column_header"):
        header, body = cells[0], cells[1:]
    else:
        header, body = [""] * width, cells

    out = ["| " + " | ".join(header) + " |",
           "| " + " | ".join(["---"] * width) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(out)


# ============================== 노트 만들기 ==============================
def build_frontmatter(page: dict, title: str, extra: dict = None) -> str:
    lines = ["---", f'title: "{yaml_escape(title)}"']

    created = (page.get("created_time") or "")[:10]
    edited = (page.get("last_edited_time") or "")[:10]
    if created:
        lines.append(f"created: {created}")
    if edited:
        lines.append(f"updated: {edited}")

    props = page.get("properties") or {}
    for name, prop in props.items():
        if prop.get("type") == "title":
            continue
        value = property_to_value(prop)
        if value is None or value == "" or value == []:
            continue
        key = _yaml_key(name)
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        elif isinstance(value, list):
            items = ", ".join(f'"{yaml_escape(v)}"' for v in value if v not in (None, ""))
            if items:
                lines.append(f"{key}: [{items}]")
        else:
            lines.append(f'{key}: "{yaml_escape(value)}"')

    for key, value in (extra or {}).items():
        lines.append(f'{key}: "{yaml_escape(value)}"' if isinstance(value, str) else f"{key}: {value}")

    if page.get("url"):
        lines.append(f'source: {page["url"]}')
    lines.append(f'notion_id: {page.get("id", "")}')
    lines.append("---")
    return "\n".join(lines)


RESERVED_KEYS = {"title", "created", "updated", "source", "notion_id", "tags", "aliases", "cssclasses"}


def _yaml_key(name: str) -> str:
    key = re.sub(r"[^0-9A-Za-z가-힣_ -]", "", name or "").strip().replace(" ", "_")
    if not key:
        return "속성"
    if key.lower() in RESERVED_KEYS and key.lower() != "tags":
        key = f"{key}_"
    return key


def unique_note_name(name: str, used: set) -> str:
    base = sanitize_filename(name)
    candidate, n = base, 2
    while candidate.lower() in used:
        candidate = f"{base} ({n})"
        n += 1
    used.add(candidate.lower())
    return candidate


# ============================== 전체 실행 ==============================
def run_notion_migration(settings: dict, log, progress, should_stop):
    out_dir = Path(settings["out_dir"])
    attachments_dir = out_dir / "attachments"
    out_dir.mkdir(parents=True, exist_ok=True)

    client = NotionClient(settings["notion_token"])

    me = client.whoami()
    workspace = (me.get("bot") or {}).get("workspace_name") or "노션"
    log(f"연결됨: {me.get('name')} · 워크스페이스 「{workspace}」")

    log("페이지 목록을 가져옵니다...")
    found = client.search()
    if not found:
        raise RuntimeError(
            "통합에 연결된 페이지가 하나도 없습니다.\n\n"
            "노션에서 옮기고 싶은 최상위 페이지를 열고\n"
            "우측 상단 ··· → 연결(Connections) → 통합 이름을 추가해 주세요.\n"
            "하위 페이지는 자동으로 따라옵니다."
        )

    objects = {item["id"]: item for item in found}
    databases = {i: o for i, o in objects.items() if o.get("object") == "database"}
    log(f"페이지 {len(objects) - len(databases)}개 · 데이터베이스 {len(databases)}개를 찾았습니다.")

    # 데이터베이스 행(=페이지)도 모두 목록에 넣는다
    for db_id, db in databases.items():
        if should_stop():
            raise Stopped()
        try:
            rows = client.query_database(db_id)
        except RuntimeError as e:
            log(f"  [건너뜀] 데이터베이스 「{page_title(db)}」 - {e}")
            continue
        for row in rows:
            objects.setdefault(row["id"], row)
        log(f"  「{page_title(db)}」 항목 {len(rows)}개")

    paths = _plan_paths(objects, databases, out_dir, log)

    link_map = {}          # notion id -> 노트 이름 (위키링크용)
    for oid, info in paths.items():
        link_map[oid] = info["note_name"]
        link_map[oid.replace("-", "")] = info["note_name"]

    file_session = requests.Session()
    stats = {"pages": 0, "files": 0, "failed": 0, "skipped": 0}
    started = time.time()

    queue = deque(paths.keys())
    total = len(queue)
    done = 0
    log(f"\n노트 {total}개를 저장합니다 -> {out_dir}")
    progress(0, total, "")

    while queue:
        if should_stop():
            raise Stopped()

        oid = queue.popleft()
        info = paths[oid]
        obj = objects[oid]
        done += 1

        target = info["path"]
        if target.exists() and settings.get("skip_existing", True):
            stats["skipped"] += 1
            progress(done, total, info["note_name"])
            continue

        try:
            ctx = {
                "downloads": [],
                "file_names": {},
                "discovered": [],
                "download_external": settings.get("download_files", True),
            }

            if obj.get("object") == "database":
                body = _database_hub_body(client, obj, oid, objects, paths)
                front = build_frontmatter(
                    {"id": oid, "url": obj.get("url"),
                     "created_time": obj.get("created_time"),
                     "last_edited_time": obj.get("last_edited_time")},
                    info["title"], {"type": "database"})
            else:
                blocks = client.children(oid)
                body = blocks_to_markdown(client, blocks, ctx, should_stop)
                front = build_frontmatter(obj, info["title"])

                if settings.get("download_files", True):
                    stats["files"] += download_files(
                        file_session, ctx["downloads"], attachments_dir, log)

            body = _resolve_page_refs(body, link_map)
            front = _resolve_page_refs(front, link_map)

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{front}\n\n{body}\n", encoding="utf-8")
            stats["pages"] += 1

            elapsed = time.time() - started
            eta = elapsed / done * (total - done)
            log(f"[{done}/{total}] {info['note_name']}  · 남은 시간 약 {eta / 60:.1f}분")

        except Stopped:
            raise
        except Exception as e:
            log(f"[{done}/{total}] [에러] {info['note_name']} -> {e}")
            stats["failed"] += 1

        progress(done, total, info["note_name"])

    log(f"\nAPI 호출 {client.calls}회")
    return {
        "elapsed": time.time() - started,
        "saved": stats["pages"],
        "failed": stats["failed"],
        "skipped": stats["skipped"],
        "images": stats["files"],
        "out_dir": out_dir,
    }


def _plan_paths(objects: dict, databases: dict, out_dir: Path, log) -> dict:
    """노션 계층 구조를 폴더 구조로 옮길 계획을 세운다."""
    parents = {}
    for oid, obj in objects.items():
        parent = obj.get("parent") or {}
        ptype = parent.get("type")
        pid = None
        if ptype == "page_id":
            pid = parent.get("page_id")
        elif ptype == "database_id":
            pid = parent.get("database_id")
        elif ptype == "block_id":
            pid = parent.get("block_id")
        parents[oid] = pid if pid in objects else None

    # 부모 -> 자식
    children = {}
    for oid, pid in parents.items():
        children.setdefault(pid, []).append(oid)

    paths, used_names = {}, {}

    def walk(oid: str, folder: Path, depth: int):
        if depth > 10:
            folder = out_dir / "_깊은_페이지"

        obj = objects[oid]
        title = page_title(obj)
        used = used_names.setdefault(str(folder).lower(), set())
        note_name = unique_note_name(title, used)

        # 노트는 항상 현재 폴더에. 하위가 있으면 같은 이름의 폴더를 옆에 만든다.
        # (노션 공식 내보내기와 같은 모양)
        paths[oid] = {
            "title": title,
            "note_name": note_name,
            "path": folder / f"{note_name}.md",
        }
        for kid in children.get(oid, []):
            walk(kid, folder / note_name, depth + 1)

    for root in children.get(None, []):
        walk(root, out_dir, 0)

    orphans = set(objects) - set(paths)
    if orphans:
        misc = out_dir / "_기타"
        for oid in orphans:
            title = page_title(objects[oid])
            used = used_names.setdefault(str(misc).lower(), set())
            note_name = unique_note_name(title, used)
            paths[oid] = {"title": title, "note_name": note_name,
                          "path": misc / f"{note_name}.md"}
        log(f"  상위를 못 찾은 {len(orphans)}개는 _기타 폴더에 넣습니다.")

    return paths


def _database_hub_body(client, db: dict, db_id: str, objects: dict, paths: dict) -> str:
    """데이터베이스는 항목을 모아 보여주는 색인 노트로 만든다."""
    desc = rich_text_to_md(db.get("description"))
    rows = [oid for oid, obj in objects.items()
            if (obj.get("parent") or {}).get("database_id") in (db_id, db_id.replace("-", ""))]

    lines = []
    if desc:
        lines += [desc, ""]
    lines.append(f"항목 {len(rows)}개")
    lines.append("")
    for oid in sorted(rows, key=lambda x: paths.get(x, {}).get("note_name", "")):
        name = paths.get(oid, {}).get("note_name")
        if name:
            lines.append(f"- [[{name}]]")
    return "\n".join(lines)


def _resolve_page_refs(text: str, link_map: dict) -> str:
    def replace(match):
        target = link_map.get(match.group(1)) or link_map.get(match.group(1).replace("-", ""))
        return f"[[{target}]]" if target else ""
    return PAGE_REF_RE.sub(replace, text)


# ============================== 토큰 확인 ==============================
def check_token(token: str) -> dict:
    """토큰이 살아있는지, 연결된 페이지가 몇 개인지 빠르게 확인한다."""
    client = NotionClient(token)
    me = client.whoami()
    found = client.request("POST", "/search", json={"page_size": 100})
    results = found.get("results", [])
    return {
        "name": me.get("name"),
        "workspace": (me.get("bot") or {}).get("workspace_name"),
        "shared": len(results),
        "has_more": found.get("has_more", False),
    }
