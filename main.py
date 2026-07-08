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

# Windows 控制台默认编码是 GBK，无法输出 emoji 和部分中文字符
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ============================================================
# 配置
# ============================================================

BASE_URL = "https://alhs.xyz"
LIST_PAGE = f"{BASE_URL}/index.php/all-post-with-nav/page/{{}}/"  # 全部文章列表页模板
OUTPUT_DIR = "novels"           # 输出目录
PROGRESS_FILE = "progress.json" # 断点续传进度文件
MIN_WORDS = 500                 # 最低字数阈值
EXCLUDED_CATEGORY = "互动小说"   # 排除的分类
DELAY = 0.5                     # 请求重试间隔（秒）
TIMEOUT = 30                    # HTTP 超时（秒）
MAX_RETRIES = 3                 # 最大重试次数
DEFAULT_WORKERS = 6             # 默认并行线程数

# 随机 UA 池，每次请求随机选取以规避反爬检测
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# ============================================================
# 全局速率限制 —— 所有线程共享，防止触发服务器防火墙
# ============================================================

class RateLimiter:
    """
    令牌桶速率限制器。
    多线程共享同一个实例，acquire() 保证全局 QPS 不超过 max_rps。
    遇到 403 后自动指数退避，成功后逐步恢复。
    """

    def __init__(self, max_rps: float = 4):
        self._interval = 1.0 / max_rps   # 两次请求之间的最小间隔
        self._last = 0.0                  # 上次请求时间戳
        self._backoff = 0.0               # 403 退避导致的额外等待
        self._fails = 0                   # 累计 403 计数（浮点，逐步衰减）
        self._lock = Lock()

    def acquire(self):
        """获取令牌——阻塞直到可以发送下一个请求"""
        with self._lock:
            # 取"正常间隔"和"退避惩罚"中的较大值
            wait = max(self._interval - (time.time() - self._last), self._backoff)
            self._backoff = 0  # 一次性消耗
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def report_403(self):
        """收到 403 后触发指数退避：暂停 10s→20s→40s…（上限 120s），同时拉大基础间隔"""
        with self._lock:
            self._fails += 1
            self._backoff = min(5 * (2 ** (self._fails - 1)), 120)
            self._interval = min(self._interval * 1.5, 3.0)

    def report_ok(self):
        """请求成功后逐步衰减失败计数，恢复正常速度"""
        with self._lock:
            self._fails = max(0, self._fails - 0.1)


rate_limiter = RateLimiter(max_rps=4)  # 全局推荐 3~5 QPS


def _random_headers() -> dict:
    """每次请求生成随机 UA + Referer 伪装"""
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": BASE_URL + "/",
    }

# ============================================================
# 工具函数
# ============================================================


def safe_filename(name: str) -> str:
    """清理文件名中的 Windows 非法字符，压缩空白，截断到 200 字符"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", name)
    return re.sub(r"\s+", " ", name).strip()[:200]


def http_get(url: str) -> requests.Response | None:
    """
    带速率控制和重试的 HTTP GET。
    403 → 触发全局退避后重试；其他错误 → 随机延迟后重试。
    全部重试耗尽 → 返回 None。
    """
    for i in range(MAX_RETRIES):
        rate_limiter.acquire()  # 全局速率控制
        try:
            resp = requests.get(url, headers=_random_headers(), timeout=TIMEOUT)
        except requests.RequestException:
            time.sleep(DELAY * (i + 1) * random.uniform(0.8, 1.5))
            continue
        if resp.status_code == 403:
            rate_limiter.report_403()
            print("\n  ⚠ 403，自动降速")
            time.sleep(10 * (i + 1))
            continue
        if not resp.ok:
            time.sleep(DELAY * (i + 1))
            continue
        rate_limiter.report_ok()
        return resp
    return None


def load_progress() -> dict:
    """加载进度文件，不存在则返回空进度"""
    return json.load(open(PROGRESS_FILE, "r", encoding="utf-8")) if os.path.exists(PROGRESS_FILE) else {
        "completed_urls": [], "current_page": 1, "collected_urls": [], "processed_series": []
    }


def save_progress(progress: dict):
    """持久化进度到 JSON 文件"""
    json.dump(progress, open(PROGRESS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ============================================================
# 步骤 1：收集全部文章链接
# ============================================================


def _parse_list_page(html: str) -> list[str]:
    """从列表页 HTML 中提取所有文章链接"""
    soup = BeautifulSoup(html, "lxml")
    return [a["href"] for a in soup.select("article a.post-title") if a.get("href")]


def _discover_last_page(start: int) -> int:
    """
    抓取首页，从分页导航中推断最后一页页码。
    例如首页底部有 "/page/145/"，则返回 145。
    """
    resp = http_get(LIST_PAGE.format(start))
    if not resp:
        return start
    pages = [int(m.group(1)) for a in BeautifulSoup(resp.text, "lxml").find_all("a", href=True)
             if (m := re.search(r"/page/(\d+)/", a["href"]))]
    return max(pages) if pages else start


def collect_urls(resume: dict | None = None, start: int = 1, end: int = 0,
                 workers: int = 10) -> list[str]:
    """
    收集全部文章 URL。
    - 串行探测首页 → 得到总页数
    - 剩余页并行抓取（ThreadPoolExecutor）
    - 支持断点续传：传入 resume 进度可跳过已完成页面
    """
    urls = []
    done = set()

    # 从进度恢复
    if resume:
        done = set(resume.get("completed_urls", []))
        start = max(resume.get("current_page", start), start)
        urls = resume.get("collected_urls", [])

    # 确定页码范围：指定 end 优先，否则探测最后一页
    last_page = end if end > 0 else _discover_last_page(start)
    pages = list(range(start, last_page + 1))

    if not pages:
        return urls

    print(f"📄 扫描第 {start}~{last_page} 页（{len(pages)} 页，{workers} 线程）...")

    # 并行抓取所有列表页，每 50 页汇报一次进度
    lock = Lock()
    done_count = 0

    def _fetch_one(p):
        nonlocal done_count
        resp = http_get(LIST_PAGE.format(p))
        result = _parse_list_page(resp.text) if resp else []
        with lock:
            done_count += 1
            if done_count % 50 == 0 or done_count == len(pages):
                print(f"  ... {done_count}/{len(pages)} 页")
        return result

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for page_urls in ex.map(_fetch_one, pages):
            for href in page_urls:
                if href not in done and href not in urls:
                    urls.append(href)

    save_progress({**load_progress(), "collected_urls": urls, "current_page": last_page + 1})
    print(f"📊 共收集 {len(urls)} 篇文章链接")
    return urls


# ============================================================
# 步骤 2：解析文章详情页
# ============================================================


def _tag_text(el, default=""):
    """
    提取元素文本。
    如果元素内部有 <a>，取 <a> 的文本（因为有些元数据是嵌套的）。
    """
    if not el:
        return default
    a = el.find("a")
    return (a or el).get_text(strip=True)


def _extract_meta(soup) -> dict:
    """从文章页 soup 提取元数据：标题、作者、日期、分类、字数"""
    cat_div = soup.find(class_="post-meta-detail-categories")
    categories = [a.get_text(strip=True) for a in cat_div.find_all("a")] if cat_div else []

    wc = 0
    for div in soup.find_all(class_="post-meta-detail-words"):
        if m := re.search(r"(\d+)\s*字", div.get_text(strip=True)):
            wc = int(m.group(1))
            break

    return {
        "title": _tag_text(soup.find("a", class_="post-title"), "未命名"),
        "author": _tag_text(soup.find(class_="post-meta-detail-author")),
        "date": _tag_text(soup.find(class_="post-meta-detail-time")),
        "category": ",".join(categories),
        "categories": categories,
        "word_count": wc,
    }


def _check_filter(meta: dict, content_len: int, skip: bool) -> str:
    """
    检查文章是否应被过滤。
    返回过滤原因（空字符串 = 不过滤）。
    skip=True 时跳过检查（用于系列章节下载）。
    """
    if skip:
        return ""
    if EXCLUDED_CATEGORY in meta["categories"]:
        return f"「{meta['title']}」互动小说"
    wc = meta["word_count"]
    if wc > 0 and wc < MIN_WORDS:
        return f"「{meta['title']}」{wc} 字 < {MIN_WORDS}"
    if wc == 0 and content_len < MIN_WORDS:
        return f"「{meta['title']}」实际 {content_len} 字 < {MIN_WORDS}"
    return ""


# 文章正文区域中需要跳过的非内容元素
_SKIP_SELECTORS = [
    "#toc",                          # 文章目录
    ".post-series",                  # 章节列表（在 _extract_chapters 中单独处理）
    ".post-series-nav",              # 章节上下页导航
    ".saboxplugin-wrap",             # 作者信息框
    "#related_posts",                # 相关推荐
    ".wpulike",                      # 点赞
    ".wp_ulike_general_class",       # 点赞子组件
    ".post-tags",                    # 标签
    ".additional-content-after-post",# 版权声明
]


def _extract_content(soup: BeautifulSoup) -> str:
    """
    从文章页提取正文文本。
    兼容三种段落格式：
      1. <p> — 标准 WordPress
      2. <div class="ace-line"> — 飞书/Lark 编辑器
      3. <div> 裸标签 — 旧版自定义格式
    <br/> 始终作为段落分隔符。
    """
    container = soup.find("div", class_="post-content")
    if not container:
        return ""

    # 移除所有非正文区域
    for sel in _SKIP_SELECTORS:
        for el in container.select(sel):
            el.decompose()

    # 移除空 div、元数据标记、反馈/举报链接
    for div in container.find_all("div"):
        text = div.get_text(strip=True)
        if not text or text == "[pilipili]" or ("反馈/举报" in text and len(text) < 30):
            div.decompose()

    parts = []
    for tag in container.find_all(["p", "div"]):
        if tag.name == "div":
            classes = tag.get("class", [])
            has_ace = "ace-line" in classes
            # 跳过非内容 div：
            #   - 有 class 但不是 ace-line → 功能区域
            #   - 是 ace-line 但包含嵌套 ace-line → 外层包装
            #   - 无 class 但包含子 div → 非叶子节点
            if (classes and not has_ace) \
               or (has_ace and tag.find("div", class_="ace-line")) \
               or (not classes and tag.find("div")):
                continue

        # 将 <br/> 转为换行，然后按行拆分段落
        for br in tag.find_all("br"):
            br.replace_with("\n")
        parts.extend(t for s in tag.get_text().strip().split("\n") if (t := s.strip()))

    return "\n\n".join(parts)


def _extract_chapters(soup: BeautifulSoup, current_url: str) -> tuple | None:
    """
    如果文章有章节列表，返回 (系列名称, 系列slug, [(url, 标题), ...])。
    列表按章节顺序排列，包含当前页面（无 <a> 链接的当前章节）。
    无章节时返回 None。
    """
    container = soup.find("div", class_="post-content")
    series = container and container.find(class_="post-series")
    if not series:
        return None

    # 系列名称和 slug（从系列专题链接提取，如 /series/mi-shi/）
    name, slug = "未命名系列", ""
    if title_el := series.find(class_="post-series-title"):
        if link := title_el.find("a", href=lambda h: h and "/series/" in h):
            name, slug = link.get_text(strip=True), link["href"]
    slug = m.group(1) if slug and (m := re.search(r"/series/([^/]+)/", slug)) else ""

    # 提取每个章节的 URL 和标题
    chapters = []
    for item in series.find_all("li", class_="post-series-item"):
        if not (item_title := item.find(class_="post-series-item-title")):
            continue
        ch_title = item_title.get_text(strip=True)
        link = item_title.find("a", href=True)
        # 有链接 = 其他章节，无链接 = 当前页面
        chapters.append(
            (link["href"], ch_title) if (link and "/archives/" in link["href"])
            else (current_url, _tag_text(soup.find("a", class_="post-title"), ch_title))
        )

    return (name, slug, chapters)


def _download_page(url: str, skip_filter: bool = False) -> tuple:
    """
    下载并解析单篇文章页。
    返回 (article_dict | None, status, reason)
      status: "ok" / "filtered" / "network_error"
    """
    resp = http_get(url)
    if not resp:
        return None, "network_error", ""

    soup = BeautifulSoup(resp.text, "lxml")
    meta = _extract_meta(soup)
    # 章节检测必须在正文提取之前——正文提取会 decompose .post-series
    chapters = _extract_chapters(soup, url)
    content = _extract_content(soup)
    actual_len = len(content.replace("\n", "").replace(" ", ""))

    if reason := _check_filter(meta, actual_len, skip_filter):
        return None, "filtered", reason

    result = {
        "url": url, "title": meta["title"], "author": meta["author"],
        "date": meta["date"], "category": meta["category"],
        "word_count": meta["word_count"] or actual_len, "content": content,
    }
    if chapters:
        result["series_name"], result["series_id"], result["chapter_urls"] = chapters
    return result, "ok", ""


def _download_chapter(url: str) -> dict | None:
    """
    下载单个章节（不过滤，额外重试）。
    用于系列合并时下载非当前章节。返回 None 表示网络彻底失败。
    """
    for i in range(MAX_RETRIES + 1):
        ch, status, _ = _download_page(url, skip_filter=True)
        if ch and ch.get("content"):
            return ch
        if status == "network_error" and i < MAX_RETRIES:
            time.sleep(DELAY * (i + 1))
    return None


# ============================================================
# 步骤 3：保存为 txt
# ============================================================


def save_txt(article: dict) -> str:
    """保存文章为 UTF-8 txt 文件，返回文件路径"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, safe_filename(article["title"]) + ".txt")
    header = (
        f"标题：{article['title']}\n作者：{article['author']}\n"
        f"日期：{article['date']}\n分类：{article['category']}\n"
        f"字数：{article['word_count']}\n原文：{article['url']}\n\n{'=' * 60}\n\n"
    )
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(header + article["content"])
    return filepath


# ============================================================
# 并行下载工作函数 (ThreadPoolExecutor worker)
# ============================================================


def _worker(url: str, completed: set, merged_series: set,
            processing_series: set, lock: Lock, idx: int) -> dict:
    """
    单个 URL 的下载 worker（多线程安全）。

    流程：
      1. 原子认领 URL → 2. 下载页面 → 3. 过滤/网络错误处理
      → 4. 普通文章直接保存 / 系列文章下载全章合并保存

    线程安全关键：
      - completed: 记录已处理 URL（含已下载、已过滤）
      - merged_series: 已完成合并的系列 slug
      - processing_series: 正在下载中的系列 slug（防止多线程冲突）
      - 系列章节逐章标记完成（先下载再标记），崩溃后可续传
    """
    # ── 1. 原子认领 URL ──
    with lock:
        if url in completed:
            return {"kind": "skip", "reason": f"已被认领: {url}", "idx": idx}
        completed.add(url)  # 先占位，网络失败会 discard

    # ── 2. 下载 + 过滤检查 ──
    article, status, reason = _download_page(url)

    if article is None:
        kind = "fail" if status == "network_error" else "skip"
        if kind == "fail":
            with lock:
                completed.discard(url)  # 网络错误放回队列，下次重试
            reason = f"网络错误: {url}"
        return {"kind": kind, "reason": reason, "idx": idx}

    # ── 3. 普通文章 → 直接保存 ──
    if not article.get("chapter_urls"):
        return {"kind": "success", "article": article, "path": save_txt(article), "idx": idx}

    # ── 4. 系列文章 → 下载全部章节 → 合并保存 ──
    sid = article["series_id"]       # 系列唯一标识，如 "mi-shi"
    sname = article.get("series_name", article["title"])
    ch_urls = article["chapter_urls"]

    with lock:
        # 检查是否有其他线程已处理或正在处理此系列
        if sid and (sid in merged_series or sid in processing_series):
            state = "已合并" if sid in merged_series else "处理中"
            return {"kind": "skip", "reason": f"系列「{sname}」{state}", "idx": idx}
        if sid:
            processing_series.add(sid)  # 认领系列

    # 遍历下载每一章（包括其他章节 URL）
    chapters_content = []
    total_wc = 0
    for ch_url, ch_title in ch_urls:
        if ch_url == url:
            # 当前章节数据已在 article 中
            chapters_content.append((ch_title, article["content"]))
            total_wc += article["word_count"]
        else:
            ch = _download_chapter(ch_url)
            chapters_content.append((ch_title, ch["content"] if ch else "[下载失败]"))
            if ch:
                total_wc += ch["word_count"]
        # 逐章标记完成——即使中途崩溃，已下载的章节不会丢失
        with lock:
            completed.add(ch_url)

    # 系列处理完毕，从 processing 移入 merged
    with lock:
        if sid:
            processing_series.discard(sid)
            merged_series.add(sid)

    if total_wc < MIN_WORDS:
        return {"kind": "skip", "reason": f"系列「{sname}」总字数 {total_wc} < {MIN_WORDS}", "idx": idx}

    # 合并：每章以 "第N章 标题" 开头，分隔线隔开正文
    merged = "\n".join(
        f"第{j + 1}章 {t}\n{'-' * 40}\n{c}\n"
        for j, (t, c) in enumerate(chapters_content)
    )
    merged_article = {
        "url": url, "title": sname, "author": article["author"],
        "date": article["date"], "category": article["category"],
        "word_count": total_wc, "content": merged,
    }
    return {"kind": "merged", "article": merged_article, "path": save_txt(merged_article),
            "ch_count": len(ch_urls), "idx": idx}


# ============================================================
# 主流程
# ============================================================

# 结果打印格式：根据 worker 返回的 kind 字段查表
_PRINT_FMT = {
    "success": lambda r: f"[{r['idx']}/{r['total']}] ✅ {r['article']['title']}\n"
                         f"     {r['article']['author']} | {r['article']['word_count']} 字 | {r['path']}",
    "merged":  lambda r: f"[{r['idx']}/{r['total']}] 📖 合并 {r['ch_count']} 章: {r['article']['title']}\n"
                         f"     {r['article']['author']} | {r['article']['word_count']} 字 | {r['path']}",
    "skip":    lambda r: f"[{r['idx']}/{r['total']}] ⏭ {r['reason']}",
    "fail":    lambda r: f"[{r['idx']}/{r['total']}] ❌ {r['reason']}",
}


def _save_checkpoint(progress, completed, all_urls, merged_series, lock):
    """线程安全地保存进度快照到 progress.json"""
    with lock:
        progress.update(
            completed_urls=list(completed),
            collected_urls=all_urls,
            processed_series=list(merged_series),
        )
        save_progress(progress)


def main():
    # ── 命令行参数 ──
    parser = argparse.ArgumentParser(description="艾利浩斯图书馆小说爬虫")
    for flag, default, hlp in [
        ("--reset", None, "清除进度从头开始"),
        ("--collect-only", None, "只收集链接不下载"),
        ("--start-page", 1, "起始列表页"),
        ("--end-page", 0, "结束列表页（0=到最后）"),
        ("--max-pages", 0, "最多扫描页数"),
        ("--max-articles", 0, "最多下载篇数（测试用）"),
        ("--workers", DEFAULT_WORKERS, f"线程数（默认 {DEFAULT_WORKERS}）"),
    ]:
        kwargs = {"help": hlp}
        if default is None:
            kwargs["action"] = "store_true"   # 布尔 flag，如 --reset
        else:
            kwargs["type"] = type(default)
            kwargs["default"] = default
        parser.add_argument(flag, **kwargs)
    args = parser.parse_args()

    start_page, end_page = args.start_page, args.end_page
    if args.max_pages > 0 and end_page == 0:
        end_page = start_page + args.max_pages - 1

    # ── 初始化 ──
    if args.reset and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
        print("🔄 进度已清除")

    progress = load_progress()
    completed = set(progress.get("completed_urls", []))       # 已处理的 URL
    merged_series = set(progress.get("processed_series", [])) # 已合并的系列 slug

    print("=" * 60)
    print(f"📚 艾利浩斯图书馆 小说爬虫")
    print(f"   输出: {OUTPUT_DIR}/  |  第 {start_page}~{'最后' if end_page == 0 else end_page} 页")
    print(f"   过滤: 字数<{MIN_WORDS} 或 互动小说  |  章节: 自动合并  |  线程: {args.workers}")
    print(f"   进度: 已下载 {len(completed)} 篇")
    print("=" * 60)

    # ── 第一步：收集文章链接 ──
    print("\n🔍 第一步：收集文章链接...")
    ranged = args.start_page != 1 or args.end_page != 0 or args.max_pages > 0

    all_urls = progress.get("collected_urls", [])
    if not all_urls or ranged:
        if all_urls and ranged:
            print("📌 页码范围变更，重新收集...")
        # 用户显式指定范围时不用旧进度数据，避免页码错乱
        all_urls = collect_urls(None if ranged else progress, start_page, end_page,
                                workers=args.workers)
        save_progress({**progress, "collected_urls": all_urls, "current_page": start_page})
    else:
        print(f"📌 从进度加载 {len(all_urls)} 个链接")

    if args.collect_only:
        return print(f"\n✅ 共 {len(all_urls)} 个链接")

    # ── 待下载列表 ──
    pending = [u for u in all_urls if u not in completed]
    if args.max_articles > 0:
        pending = pending[:args.max_articles]
        print(f"\n📌 限制: --max-articles={args.max_articles}")
    print(f"\n📌 待下载: {len(pending)} 篇")

    if not pending:
        return print("✅ 全部完成！")

    # ── 第二步：并行下载 ──
    print(f"\n📥 第二步：并行下载（{args.workers} 线程）...\n" + "-" * 40)

    stats = {"success": 0, "merged": 0, "skip": 0, "fail": 0}
    lock = Lock()          # 保护 shared state (completed, merged_series, processing_series)
    print_lock = Lock()    # 防止多线程打印消息交错
    processing_series = set()
    total = len(pending)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        # 一次性提交所有任务
        futures = {ex.submit(_worker, url, completed, merged_series,
                             processing_series, lock, i + 1): i + 1
                   for i, url in enumerate(pending)}

        # 按完成顺序收结果
        for f in as_completed(futures):
            try:
                r = f.result()
            except Exception as e:
                with print_lock:
                    print(f"[{futures[f]}/{total}] ❌ 异常: {e}")
                stats["fail"] += 1
                continue

            r["total"] = total  # worker 不知道 total，在这里补上
            kind = r["kind"]

            with print_lock:
                print(_PRINT_FMT.get(kind, lambda r: f"未知: {r}")(r))
                stats[kind] += 1

            # 每 25 篇存一次进度
            if r["idx"] % 25 == 0:
                _save_checkpoint(progress, completed, all_urls, merged_series, lock)
                with print_lock:
                    print(f"  💾 已保存（成功 {stats['success']} | 合并 {stats['merged']} | "
                          f"跳过 {stats['skip']} | 失败 {stats['fail']}）")

    # ── 最终存盘 ──
    _save_checkpoint(progress, completed, all_urls, merged_series, lock)

    print(f"\n{'=' * 60}")
    print("🎉 完成！")
    print(f"   单篇: {stats['success']}  |  合并: {stats['merged']}  |  "
          f"跳过: {stats['skip']}  |  失败: {stats['fail']}")
    print(f"   位置: {os.path.abspath(OUTPUT_DIR)}/")
    print("=" * 60)


if __name__ == "__main__":
    main()
