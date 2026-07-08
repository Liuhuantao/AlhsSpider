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
"""

import requests
from bs4 import BeautifulSoup
import time, os, re, json, sys, argparse

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
DELAY = 1.5
TIMEOUT = 30
MAX_RETRIES = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# ============================================================
# 工具函数
# ============================================================


def safe_filename(name: str) -> str:
    """清理文件名中的非法字符"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:200] if len(name) > 200 else name


def http_get(url: str) -> requests.Response | None:
    """带重试的 HTTP GET 请求"""
    for i in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            print(f"  ⚠ 请求失败 ({i + 1}/{MAX_RETRIES}): {e}")
            if i < MAX_RETRIES - 1:
                time.sleep(DELAY * (i + 1))
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


def collect_urls(resume: dict | None = None, start: int = 1, end: int = 0) -> list[str]:
    """遍历列表页收集全部文章 URL"""
    urls = []
    done = set()

    if resume:
        done = set(resume.get("completed_urls", []))
        if start == 1:
            start = max(resume.get("current_page", 1), 1)
        urls = resume.get("collected_urls", [])

    page, empty_streak = start, 0

    while True:
        if end > 0 and page > end:
            print(f"--end-page={end} 已达到，停止扫描")
            break

        print(f"📄 扫描: {LIST_PAGE.format(page)}", end=" ")
        resp = http_get(LIST_PAGE.format(page))

        if resp is None:
            empty_streak += 1
            print("❌")
            if empty_streak >= 3:
                break
            page += 1
            time.sleep(DELAY)
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        new = 0
        for article in soup.find_all("article"):
            link = article.find("a", class_="post-title")
            if link and link.get("href"):
                href = link["href"]
                if href not in done and href not in urls:
                    urls.append(href)
                    new += 1

        print(f"→ {new} 篇，累计 {len(urls)} 篇")

        if new == 0:
            empty_streak += 1
            if empty_streak >= 3:
                print("✅ 已到达最后一页")
                break
        else:
            empty_streak = 0

        if page % 10 == 0:
            save_progress({**load_progress(), "collected_urls": urls, "current_page": page + 1})

        page += 1
        time.sleep(DELAY)

    print(f"\n📊 共收集 {len(urls)} 篇文章链接")
    return urls


# ============================================================
# 步骤 2：解析文章
# ============================================================


def _find_text(soup, selector, default=""):
    """在 soup 中查找元素并提取文本"""
    el = soup.find(**selector) if isinstance(selector, dict) else soup.find(selector)
    if not el:
        return default
    # 如果元素内有 <a>，提取 <a> 的文本，否则用自身文本
    a = el.find("a")
    return a.get_text(strip=True) if a else el.get_text(strip=True)


def _extract_meta(soup) -> dict:
    """从文章页提取元数据"""
    title = _find_text(soup, {"class_": "post-title"}, "未命名")

    # 分类
    cat_div = soup.find(class_="post-meta-detail-categories")
    categories = [a.get_text(strip=True) for a in cat_div.find_all("a")] if cat_div else []

    # 字数
    wc = 0
    for div in soup.find_all(class_="post-meta-detail-words"):
        m = re.search(r"(\d+)\s*字", div.get_text(strip=True))
        if m:
            wc = int(m.group(1))
            break

    # 作者
    author_div = soup.find(class_="post-meta-detail-author")
    author = author_div.find("a").get_text(strip=True) if author_div and author_div.find("a") else ""

    # 日期
    time_div = soup.find(class_="post-meta-detail-time")
    date = time_div.find("time").get_text(strip=True) if time_div and time_div.find("time") else ""

    return {
        "title": title,
        "author": author,
        "date": date,
        "category": ",".join(categories),
        "categories": categories,
        "word_count": wc,
    }


# 非正文区域的 CSS 选择器
_SKIP_SELECTORS = [
    "#toc", ".post-series", ".post-series-nav",
    ".saboxplugin-wrap", "#related_posts", ".wpulike",
    ".wp_ulike_general_class", ".post-tags",
    ".additional-content-after-post",
]


def _extract_content(soup: BeautifulSoup) -> str:
    """从文章页提取正文文本"""
    container = soup.find("div", class_="post-content")
    if not container:
        return ""

    # 移除所有非正文区域
    for sel in _SKIP_SELECTORS:
        for el in container.select(sel):
            el.decompose()

    # 移除空 div / 元数据 div
    for div in container.find_all("div"):
        text = div.get_text(strip=True)
        if not text or text == "[pilipili]" or ("反馈/举报" in text and len(text) < 30):
            div.decompose()

    # 收集正文段落（支持 p、div.ace-line、裸 div 三种格式）
    parts = []
    for tag in container.find_all(["p", "div"]):
        if tag.name == "div":
            classes = tag.get("class", [])
            if classes:
                if "ace-line" not in classes:
                    continue
                if tag.find("div", class_="ace-line"):  # 跳过包装 div
                    continue
            elif tag.find("div"):  # 裸 div 只取叶子节点
                continue

        for br in tag.find_all("br"):
            br.replace_with("\n")
        for para in tag.get_text().strip().split("\n"):
            para = para.strip()
            if para:
                parts.append(para)

    return "\n\n".join(parts)


def _extract_chapters(soup: BeautifulSoup, current_url: str) -> tuple | None:
    """提取章节列表，返回 (series_name, series_id, [(url, title), ...]) 或 None"""
    container = soup.find("div", class_="post-content")
    if not container:
        return None

    series = container.find(class_="post-series")
    if not series:
        return None

    # 系列名称和 slug
    name = "未命名系列"
    slug = ""
    title_el = series.find(class_="post-series-title")
    if title_el:
        link = title_el.find("a", href=lambda h: h and "/series/" in h)
        if link:
            name = link.get_text(strip=True)
            slug = link["href"]
    if slug:
        m = re.search(r"/series/([^/]+)/", slug)
        slug = m.group(1) if m else ""

    # 提取章节
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
            page_title = soup.find("a", class_="post-title")
            chapters.append((current_url, page_title.get_text(strip=True) if page_title else ch_title))

    return (name, slug, chapters)


def download_article(url: str, skip_filter: bool = False) -> dict | None:
    """
    下载并解析一篇文章。
    返回 dict（含 title/content/word_count/等）或 None（需过滤/下载失败）。
    如果文章有章节列表，附加 series_name/series_id/chapter_urls 字段。
    """
    resp = http_get(url)
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    meta = _extract_meta(soup)

    # 过滤
    if not skip_filter:
        if EXCLUDED_CATEGORY in meta["categories"]:
            print(f"  ⏭ 跳过（互动小说）: {meta['title']}")
            return None
        if meta["word_count"] > 0 and meta["word_count"] < MIN_WORDS:
            print(f"  ⏭ 跳过（{meta['word_count']} 字 < {MIN_WORDS}）: {meta['title']}")
            return None

    # 章节检测（必须在提取正文前，正文提取会移除 .post-series）
    chapters = _extract_chapters(soup, url)

    # 正文
    content = _extract_content(soup)
    actual_len = len(content.replace("\n", "").replace(" ", ""))

    if not skip_filter and meta["word_count"] == 0 and actual_len < MIN_WORDS:
        print(f"  ⏭ 跳过（实际字数 {actual_len} < {MIN_WORDS}）: {meta['title']}")
        return None

    result = {
        "url": url,
        "title": meta["title"],
        "author": meta["author"],
        "date": meta["date"],
        "category": meta["category"],
        "word_count": meta["word_count"] or actual_len,
        "content": content,
    }
    if chapters:
        result["series_name"], result["series_id"], result["chapter_urls"] = chapters
    return result


def _download_chapter(url: str) -> dict | None:
    """下载单个章节（不过滤，带额外重试）"""
    for i in range(MAX_RETRIES + 1):
        article = download_article(url, skip_filter=True)
        if article and article.get("content"):
            return article
        if i < MAX_RETRIES:
            time.sleep(DELAY * (i + 1))
    return None


# ============================================================
# 步骤 3：保存
# ============================================================


def save_txt(article: dict) -> str:
    """保存文章为 txt，返回文件路径"""
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
    print(f"   进度: 已下载 {len(completed)} 篇")
    print("=" * 60)

    # --- 收集链接 ---
    print("\n🔍 第一步：收集文章链接...")
    ranged = args.start_page != 1 or args.end_page != 0 or args.max_pages > 0

    all_urls = progress.get("collected_urls", [])
    if not all_urls or ranged:
        if all_urls and ranged:
            print("📌 页码范围变更，重新收集...")
        all_urls = collect_urls(None if ranged else progress, start_page, end_page)
        progress.update(collected_urls=all_urls, current_page=start_page)
        save_progress(progress)
    else:
        print(f"📌 从进度加载 {len(all_urls)} 个链接")

    if args.collect_only:
        return print(f"\n✅ 共 {len(all_urls)} 个链接")

    # 待下载列表
    pending = [u for u in all_urls if u not in completed]
    if args.max_articles > 0 and len(pending) > args.max_articles:
        pending = pending[:args.max_articles]
        print(f"\n📌 限制: --max-articles={args.max_articles}")
    print(f"\n📌 待下载: {len(pending)} 篇（已完成: {len(completed)} 篇）")

    if not pending:
        return print("✅ 全部完成！")

    # --- 逐篇下载 ---
    print("\n📥 第二步：下载文章...\n" + "-" * 40)
    stats = {"success": 0, "merged": 0, "skip": 0, "fail": 0}

    for i, url in enumerate(pending):
        if url in completed:
            continue

        print(f"\n[{i + 1}/{len(pending)}] {url}")

        try:
            article = download_article(url)
        except Exception as e:
            print(f"  ❌ 异常: {e}")
            stats["fail"] += 1
            completed.add(url)
            continue

        if article is None:
            stats["skip"] += 1
            completed.add(url)
            continue

        # --- 章节合并 ---
        if article.get("chapter_urls"):
            sid = article["series_id"]
            sname = article.get("series_name", article["title"])

            if sid and sid in merged_series:
                print(f"  ⏭ 系列「{sname}」已合并过")
                stats["skip"] += 1
                completed.add(url)
                continue

            print(f"  📖 系列「{sname}」共 {len(article['chapter_urls'])} 章，合并中...")
            chapters_content = []
            total_wc = 0

            for ci, (ch_url, ch_title) in enumerate(article["chapter_urls"]):
                if ch_url == url:
                    chapters_content.append((ch_title, article["content"]))
                    total_wc += article["word_count"]
                    print(f"    [{ci + 1}/{len(article['chapter_urls'])}] {ch_title} (当前)")
                else:
                    print(f"    [{ci + 1}/{len(article['chapter_urls'])}] {ch_title} ...", end=" ", flush=True)
                    ch = _download_chapter(ch_url)
                    if ch:
                        chapters_content.append((ch_title, ch["content"]))
                        total_wc += ch["word_count"]
                        print("OK")
                    else:
                        chapters_content.append((ch_title, "[下载失败]"))
                        print("FAIL")
                    time.sleep(DELAY * 2)
                completed.add(ch_url)

            if total_wc < MIN_WORDS:
                print(f"  ⏭ 系列总字数 {total_wc} < {MIN_WORDS}")
                stats["skip"] += 1
                merged_series.add(sid) if sid else None
                continue

            # 合并
            merged_ch_count = len(article["chapter_urls"])  # 保存章节数
            merged = "\n".join(
                f"第{j + 1}章 {t}\n{'-' * 40}\n{c}\n"
                for j, (t, c) in enumerate(chapters_content)
            )
            article = {
                "url": url, "title": sname, "author": article["author"],
                "date": article["date"], "category": article["category"],
                "word_count": total_wc, "content": merged,
            }
            kind = "merged"
        else:
            merged_ch_count = 0
            kind = "success"

        try:
            path = save_txt(article)
            tag = f"合并 {merged_ch_count} 章 " if kind == "merged" else ""
            print(f"  ✅ {tag}已保存: {path}")
            print(f"     《{article['title']}》| {article['author']} | {article['word_count']} 字")
            stats[kind] += 1
        except Exception as e:
            print(f"  ❌ 保存失败: {e}")
            stats["fail"] += 1

        completed.add(url)

        # 定期存盘
        if (i + 1) % 5 == 0:
            progress.update(
                completed_urls=list(completed),
                collected_urls=all_urls,
                processed_series=list(merged_series),
            )
            save_progress(progress)
            print(f"  💾 进度已保存（成功 {stats['success']} | 合并 {stats['merged']} | 跳过 {stats['skip']} | 失败 {stats['fail']}）")

        time.sleep(DELAY)

    # 最终存盘
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
