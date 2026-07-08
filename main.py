"""
艾利浩斯图书馆 小说爬虫
=======================
爬取 https://alhs.xyz 全部文章，保存为本地 txt 文件。

过滤规则：
  - 字数 < 500 的文章跳过
  - 分类包含「互动小说」的文章跳过
  - 多章节文章自动合并为一个文件

用法：
  python main.py                              # 开始爬取（支持断点续传）
  python main.py --start-page=5 --end-page=10 # 指定页码范围
  python main.py --reset                      # 清除进度，从头开始
  python main.py --workers=8                  # 8 线程并行下载
"""

import requests
from bs4 import BeautifulSoup
import time, os, re, json, sys, argparse, random
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# Windows 控制台 UTF-8 支持
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ============================================================
# 配置
# ============================================================

BASE_URL = "https://alhs.xyz"
LIST_PAGE = f"{BASE_URL}/index.php/all-post-with-nav/page/{{}}/"
OUTPUT_DIR = "novels"
PROGRESS_FILE = "progress.json"
MIN_WORDS = 500
EXCLUDED_CATEGORY = "互动小说"
DELAY = 0.5              # 基础请求间隔
TIMEOUT = 30
MAX_RETRIES = 3
DEFAULT_WORKERS = 6

# 反屏蔽：随机 UA 池
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# 全局速率控制
class RateLimiter:
    """令牌桶风格的全局速率限制器，保证总 QPS 不超过上限"""

    def __init__(self, max_rps: float = 6):
        self.max_rps = max_rps
        self.min_interval = 1.0 / max_rps
        self.last_time = 0.0
        self.lock = Lock()
        self._403_backoff = 0.0   # 遇到 403 后全局暂停秒数
        self._403_count = 0

    def acquire(self):
        """阻塞直到可以发送下一个请求"""
        with self.lock:
            now = time.time()
            wait = self.min_interval - (now - self.last_time)
            if self._403_backoff > 0:
                wait = max(wait, self._403_backoff)
                self._403_backoff = 0
            if wait > 0:
                time.sleep(wait)
            self.last_time = time.time()

    def report_403(self):
        """收到 403 后触发指数退避"""
        with self.lock:
            self._403_count += 1
            self._403_backoff = min(5 * (2 ** (self._403_count - 1)), 120)
            self.min_interval = min(self.min_interval * 1.5, 3.0)

    def report_success(self):
        """成功请求后逐渐降低退避"""
        with self.lock:
            self._403_count = max(0, self._403_count - 0.1)

rate_limiter = RateLimiter(max_rps=4)  # 全局最大每秒请求数，建议 3-5


def _random_headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": BASE_URL + "/",
    }

# ============================================================
# 工具函数
# ============================================================


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:200] if len(name) > 200 else name


def http_get(url: str) -> requests.Response | None:
    for i in range(MAX_RETRIES):
        rate_limiter.acquire()
        try:
            resp = requests.get(url, headers=_random_headers(), timeout=TIMEOUT)
            if resp.status_code == 403:
                rate_limiter.report_403()
                print(f"\n  ⚠ 403 被限速，自动降速（全局 QPS 已下调）")
                time.sleep(10 * (i + 1))
                continue
            resp.raise_for_status()
            rate_limiter.report_success()
            return resp
        except requests.RequestException:
            if i < MAX_RETRIES - 1:
                time.sleep(DELAY * (i + 1) * random.uniform(0.8, 1.5))
    return None


def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"completed_urls": [], "current_page": 1, "collected_urls": [], "processed_series": []}


def save_progress(progress: dict):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


# ============================================================
# 步骤 1：收集文章链接
# ============================================================


def _parse_list_page(html: str) -> list[str]:
    """从列表页 HTML 提取文章 URL 列表"""
    soup = BeautifulSoup(html, "lxml")
    urls = []
    for article in soup.find_all("article"):
        link = article.find("a", class_="post-title")
        if link and link.get("href"):
            urls.append(link["href"])
    return urls


def _discover_last_page(start: int) -> int:
    """获取第一页，从分页链接中推断最后一页的页码"""
    resp = http_get(LIST_PAGE.format(start))
    if not resp:
        return start
    soup = BeautifulSoup(resp.text, "lxml")
    max_page = start
    for a in soup.find_all("a", href=True):
        m = re.search(r"/page/(\d+)/", a["href"])
        if m:
            max_page = max(max_page, int(m.group(1)))
    return max_page


def collect_urls(resume: dict | None = None, start: int = 1, end: int = 0,
                 workers: int = 10) -> list[str]:
    """收集全部文章 URL（首尾页串行探测，中间页并行抓取）"""
    urls = []
    done = set()

    if resume:
        done = set(resume.get("completed_urls", []))
        if start == 1:
            start = max(resume.get("current_page", 1), 1)
        urls = resume.get("collected_urls", [])

    # 确定最后一页
    last_page = end if end > 0 else _discover_last_page(start)
    pages = list(range(start, last_page + 1))

    if not pages:
        print("📊 无页面需要扫描")
        return urls

    print(f"📄 扫描第 {start}~{last_page} 页（共 {len(pages)} 页，{workers} 线程）...")

    # 并行抓取所有列表页
    lock = Lock()
    count = 0

    def _fetch_one(p):
        nonlocal count
        resp = http_get(LIST_PAGE.format(p))
        page_urls = _parse_list_page(resp.text) if resp else []
        with lock:
            count += 1
            if count % 50 == 0 or count == len(pages):
                print(f"  ... {count}/{len(pages)} 页")
        return page_urls

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = executor.map(_fetch_one, pages)

    for page_urls in results:
        for href in page_urls:
            if href not in done and href not in urls:
                urls.append(href)

    save_progress({**load_progress(), "collected_urls": urls, "current_page": last_page + 1})
    print(f"📊 共收集 {len(urls)} 篇文章链接")
    return urls


# ============================================================
# 步骤 2：解析文章
# ============================================================


def _extract_meta(soup) -> dict:
    title_el = soup.find("a", class_="post-title")
    title = title_el.get_text(strip=True) if title_el else "未命名"

    cat_div = soup.find(class_="post-meta-detail-categories")
    categories = [a.get_text(strip=True) for a in cat_div.find_all("a")] if cat_div else []

    wc = 0
    for div in soup.find_all(class_="post-meta-detail-words"):
        m = re.search(r"(\d+)\s*字", div.get_text(strip=True))
        if m:
            wc = int(m.group(1))
            break

    author_div = soup.find(class_="post-meta-detail-author")
    author = author_div.find("a").get_text(strip=True) if author_div and author_div.find("a") else ""

    time_div = soup.find(class_="post-meta-detail-time")
    date = time_div.find("time").get_text(strip=True) if time_div and time_div.find("time") else ""

    return {"title": title, "author": author, "date": date,
            "category": ",".join(categories), "categories": categories, "word_count": wc}


_SKIP_SELECTORS = [
    "#toc", ".post-series", ".post-series-nav",
    ".saboxplugin-wrap", "#related_posts", ".wpulike",
    ".wp_ulike_general_class", ".post-tags", ".additional-content-after-post",
]


def _extract_content(soup: BeautifulSoup) -> str:
    container = soup.find("div", class_="post-content")
    if not container:
        return ""

    for sel in _SKIP_SELECTORS:
        for el in container.select(sel):
            el.decompose()

    for div in container.find_all("div"):
        text = div.get_text(strip=True)
        if not text or text == "[pilipili]" or ("反馈/举报" in text and len(text) < 30):
            div.decompose()

    parts = []
    for tag in container.find_all(["p", "div"]):
        if tag.name == "div":
            classes = tag.get("class", [])
            if classes:
                if "ace-line" not in classes:
                    continue
                if tag.find("div", class_="ace-line"):
                    continue
            elif tag.find("div"):
                continue

        for br in tag.find_all("br"):
            br.replace_with("\n")
        for para in tag.get_text().strip().split("\n"):
            para = para.strip()
            if para:
                parts.append(para)

    return "\n\n".join(parts)


def _extract_chapters(soup: BeautifulSoup, current_url: str) -> tuple | None:
    container = soup.find("div", class_="post-content")
    if not container:
        return None
    series = container.find(class_="post-series")
    if not series:
        return None

    name, slug = "未命名系列", ""
    title_el = series.find(class_="post-series-title")
    if title_el:
        link = title_el.find("a", href=lambda h: h and "/series/" in h)
        if link:
            name, slug = link.get_text(strip=True), link["href"]
    if slug:
        m = re.search(r"/series/([^/]+)/", slug)
        slug = m.group(1) if m else ""

    chapters = []
    for item in series.find_all("li", class_="post-series-item"):
        item_title = item.find(class_="post-series-item-title")
        if not item_title:
            continue
        ch_title = item_title.get_text(strip=True)
        link = item_title.find("a", href=True)
        if link and "/archives/" in link["href"]:
            chapters.append((link["href"], ch_title))
        else:
            pt = soup.find("a", class_="post-title")
            chapters.append((current_url, pt.get_text(strip=True) if pt else ch_title))

    return (name, slug, chapters)


def _download_page(url: str, skip_filter: bool = False) -> tuple:
    """
    下载一篇文章页。
    返回 (article_dict, status, reason)。
    status: "ok" / "filtered" / "network_error"
    reason: 过滤原因描述（仅 filtered 时有意义）
    """
    resp = http_get(url)
    if not resp:
        return None, "network_error", ""

    soup = BeautifulSoup(resp.text, "lxml")
    meta = _extract_meta(soup)

    if not skip_filter:
        if EXCLUDED_CATEGORY in meta["categories"]:
            return None, "filtered", f"「{meta['title']}」互动小说"
        if meta["word_count"] > 0 and meta["word_count"] < MIN_WORDS:
            return None, "filtered", f"「{meta['title']}」{meta['word_count']} 字 < {MIN_WORDS}"

    chapters = _extract_chapters(soup, url)
    content = _extract_content(soup)
    actual_len = len(content.replace("\n", "").replace(" ", ""))

    if not skip_filter and meta["word_count"] == 0 and actual_len < MIN_WORDS:
        return None, "filtered", f"「{meta['title']}」实际 {actual_len} 字 < {MIN_WORDS}"

    result = {
        "url": url, "title": meta["title"], "author": meta["author"],
        "date": meta["date"], "category": meta["category"],
        "word_count": meta["word_count"] or actual_len, "content": content,
    }
    if chapters:
        result["series_name"], result["series_id"], result["chapter_urls"] = chapters
    return result, "ok", ""


def _download_chapter(url: str) -> dict | None:
    """下载单个章节（不过滤，额外重试）。返回 None 表示网络失败。"""
    for i in range(MAX_RETRIES + 1):
        ch, status, _ = _download_page(url, skip_filter=True)
        if ch and ch.get("content"):
            return ch
        if status == "network_error" and i < MAX_RETRIES:
            time.sleep(DELAY * (i + 1))
    return None


# ============================================================
# 步骤 3：保存
# ============================================================


def save_txt(article: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, safe_filename(article["title"]) + ".txt")
    header = (
        f"标题：{article['title']}\n"
        f"作者：{article['author']}\n"
        f"日期：{article['date']}\n"
        f"分类：{article['category']}\n"
        f"字数：{article['word_count']}\n"
        f"原文：{article['url']}\n\n"
        f"{'=' * 60}\n\n"
    )
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(header + article["content"])
    return filepath


# ============================================================
# 并行下载工作函数
# ============================================================


def _worker(url: str, completed: set, merged_series: set,
            processing_series: set, lock: Lock, idx: int) -> dict:
    """
    单个 URL 的下载工作函数（线程安全）。
    返回结果 dict: {kind, article?, path?, ch_count?, reason?, idx, url}
    """
    # ── 原子认领 URL ──
    with lock:
        if url in completed:
            return {"kind": "skip", "reason": f"已被其他线程认领: {url}",
                    "idx": idx, "url": url}
        completed.add(url)

    # ── 下载页面 ──
    article, status, reason = _download_page(url)
    if article is None:
        if status == "network_error":
            # 网络错误——放回队列，下次重试
            with lock:
                completed.discard(url)
            return {"kind": "fail", "reason": f"网络错误，已放回重试: {url}",
                    "idx": idx, "url": url}
        # 过滤——永久跳过
        return {"kind": "skip", "reason": reason, "idx": idx, "url": url}

    # ── 普通文章 ──
    if not article.get("chapter_urls"):
        path = save_txt(article)
        return {"kind": "success", "article": article, "path": path, "idx": idx, "url": url}

    # ── 系列文章 ──
    sid = article["series_id"]
    sname = article.get("series_name", article["title"])
    ch_urls = article["chapter_urls"]

    with lock:
        if sid and sid in merged_series:
            return {"kind": "skip", "reason": f"系列「{sname}」已合并", "idx": idx, "url": url}
        if sid and sid in processing_series:
            return {"kind": "skip", "reason": f"系列「{sname}」处理中", "idx": idx, "url": url}
        # 认领系列（只锁 slug，不预认领章节 URL——避免崩溃后丢失未下载的章节）
        if sid:
            processing_series.add(sid)

    # ── 下载系列全部章节 ──
    chapters_content = []
    total_wc = 0
    for _ci, (ch_url, ch_title) in enumerate(ch_urls):
        if ch_url == url:
            chapters_content.append((ch_title, article["content"]))
            total_wc += article["word_count"]
        else:
            ch = _download_chapter(ch_url)
            if ch:
                chapters_content.append((ch_title, ch["content"]))
                total_wc += ch["word_count"]
            else:
                chapters_content.append((ch_title, "[下载失败]"))
        # 每下载一章就标记完成（崩溃后可断点续传）
        with lock:
            completed.add(ch_url)

    with lock:
        if sid:
            processing_series.discard(sid)
            merged_series.add(sid)

    if total_wc < MIN_WORDS:
        return {"kind": "skip", "reason": f"系列总字数 {total_wc} < {MIN_WORDS}", "idx": idx, "url": url}

    # ── 合并保存 ──
    merged_ch_count = len(ch_urls)
    merged = "\n".join(
        f"第{j + 1}章 {t}\n{'-' * 40}\n{c}\n"
        for j, (t, c) in enumerate(chapters_content)
    )
    merged_article = {
        "url": url, "title": sname, "author": article["author"],
        "date": article["date"], "category": article["category"],
        "word_count": total_wc, "content": merged,
    }
    path = save_txt(merged_article)
    return {"kind": "merged", "article": merged_article, "path": path,
            "ch_count": merged_ch_count, "idx": idx, "url": url}


# ============================================================
# 主流程
# ============================================================


def main():
    parser = argparse.ArgumentParser(description="艾利浩斯图书馆小说爬虫")
    parser.add_argument("--reset", action="store_true", help="清除进度从头开始")
    parser.add_argument("--collect-only", action="store_true", help="只收集链接不下载")
    parser.add_argument("--start-page", type=int, default=1, help="起始列表页")
    parser.add_argument("--end-page", type=int, default=0, help="结束列表页（0=到最后）")
    parser.add_argument("--max-pages", type=int, default=0, help="最多扫描页数")
    parser.add_argument("--max-articles", type=int, default=0, help="最多下载篇数（测试用）")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"并行下载线程数（默认 {DEFAULT_WORKERS}）")
    args = parser.parse_args()

    start_page = args.start_page
    end_page = args.end_page
    if args.max_pages > 0 and end_page == 0:
        end_page = start_page + args.max_pages - 1

    if args.reset and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
        print("🔄 进度已清除")

    progress = load_progress()
    completed = set(progress.get("completed_urls", []))
    merged_series = set(progress.get("processed_series", []))

    print("=" * 60)
    print("📚 艾利浩斯图书馆 小说爬虫")
    print(f"   输出: {OUTPUT_DIR}/  |  第 {start_page}~{'最后' if end_page == 0 else end_page} 页")
    print(f"   过滤: 字数<{MIN_WORDS} 或 互动小说  |  章节: 自动合并")
    print(f"   线程: {args.workers}  |  进度: 已下载 {len(completed)} 篇")
    print("=" * 60)

    # ── 收集链接 ──
    print("\n🔍 第一步：收集文章链接...")
    ranged = args.start_page != 1 or args.end_page != 0 or args.max_pages > 0

    all_urls = progress.get("collected_urls", [])
    if not all_urls or ranged:
        if all_urls and ranged:
            print("📌 页码范围变更，重新收集...")
        all_urls = collect_urls(None if ranged else progress, start_page, end_page,
                                 workers=args.workers)
        progress.update(collected_urls=all_urls, current_page=start_page)
        save_progress(progress)
    else:
        print(f"📌 从进度加载 {len(all_urls)} 个链接")

    if args.collect_only:
        return print(f"\n✅ 共 {len(all_urls)} 个链接")

    pending = [u for u in all_urls if u not in completed]
    if args.max_articles > 0 and len(pending) > args.max_articles:
        pending = pending[:args.max_articles]
        print(f"\n📌 限制: --max-articles={args.max_articles}")
    print(f"\n📌 待下载: {len(pending)} 篇  |  线程: {args.workers}")

    if not pending:
        return print("✅ 全部完成！")

    # ── 并行下载 ──
    print(f"\n📥 第二步：并行下载（{args.workers} 线程）...\n" + "-" * 40)

    stats = {"success": 0, "merged": 0, "skip": 0, "fail": 0}
    lock = Lock()
    processing_series = set()
    total = len(pending)
    print_lock = Lock()  # 防止打印交错

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for i, url in enumerate(pending):
            f = executor.submit(_worker, url, completed, merged_series,
                                processing_series, lock, i + 1)
            futures[f] = i + 1

        for f in as_completed(futures):
            idx = futures[f]
            try:
                r = f.result()
            except Exception as e:
                with print_lock:
                    print(f"[{idx}/{total}] ❌ 线程异常: {e}")
                stats["fail"] += 1
                continue

            kind = r["kind"]
            with print_lock:
                if kind == "success":
                    a = r["article"]
                    print(f"[{idx}/{total}] ✅ {a['title']}")
                    print(f"     {a['author']} | {a['word_count']} 字 | {r['path']}")
                    stats["success"] += 1
                elif kind == "merged":
                    a = r["article"]
                    print(f"[{idx}/{total}] 📖 合并 {r['ch_count']} 章: {a['title']}")
                    print(f"     {a['author']} | {a['word_count']} 字 | {r['path']}")
                    stats["merged"] += 1
                elif kind == "skip":
                    print(f"[{idx}/{total}] ⏭ {r.get('reason', '')}")
                    stats["skip"] += 1
                elif kind == "fail":
                    print(f"[{idx}/{total}] ❌ {r.get('reason', '')}")
                    stats["fail"] += 1

            # 定期存盘
            if idx % 25 == 0:
                with lock:
                    progress.update(
                        completed_urls=list(completed),
                        collected_urls=all_urls,
                        processed_series=list(merged_series),
                    )
                    save_progress(progress)
                with print_lock:
                    print(f"  💾 进度已保存（成功 {stats['success']} | 合并 {stats['merged']} | "
                          f"跳过 {stats['skip']} | 失败 {stats['fail']}）")

    # ── 最终存盘 ──
    progress.update(
        completed_urls=list(completed),
        collected_urls=all_urls,
        processed_series=list(merged_series),
    )
    save_progress(progress)

    print(f"\n{'=' * 60}")
    print("🎉 完成！")
    print(f"   单篇: {stats['success']}  |  合并: {stats['merged']}  |  跳过: {stats['skip']}  |  失败: {stats['fail']}")
    print(f"   位置: {os.path.abspath(OUTPUT_DIR)}/")
    print("=" * 60)


if __name__ == "__main__":
    main()
