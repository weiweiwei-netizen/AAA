import os
import json
import time
import hashlib
import feedparser
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI

# ==================== 配置区 ====================
PUSHPLUS_TOKEN = os.environ["PUSHPLUS_TOKEN"]
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]

# 关键词列表 —— 按你的关注点自由增删
GOOGLE_NEWS_QUERIES = [
    "Iran US conflict",
    "Strait of Hormuz",
    "Iran war oil",
    "US Iran military",
]

OILPRICE_RSS = "https://oilprice.com/rss/main"
EAGLE_HORMUZ_API = "https://eagleintelmari.com/api/hormuz-status"

# 只保留最近 N 小时的内容，避免重复推送旧闻
HOURS_LOOKBACK = 6
# =================================================


def google_news_url(query: str) -> str:
    from urllib.parse import quote
    q = quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def fetch_rss(url: str, source_name: str) -> list[dict]:
    """抓取单个 RSS，返回标准化条目列表。"""
    try:
        feed = feedparser.parse(url)
        entries = []
        cutoff = time.time() - HOURS_LOOKBACK * 3600
        for e in feed.entries[:20]:  # 每个源最多取 20 条
            # feedparser 的 published_parsed 是 UTC 结构体
            published = time.mktime(e.published_parsed) if hasattr(e, "published_parsed") else time.time()
            if published < cutoff:
                continue
            entries.append({
                "source": source_name,
                "title": e.get("title", "").strip(),
                "summary": e.get("summary", "")[:300].strip(),
                "link": e.get("link", ""),
                "published": published,
            })
        return entries
    except Exception as exc:
        print(f"[RSS 失败] {source_name}: {exc}")
        return []


def fetch_hormuz_status() -> str:
    """拉取霍尔木兹海峡结构化状态，返回 JSON 字符串。"""
    try:
        r = requests.get(EAGLE_HORMUZ_API, timeout=15)
        r.raise_for_status()
        return json.dumps(r.json(), ensure_ascii=False)
    except Exception as exc:
        return f"霍尔木兹状态 API 暂不可达：{exc}"


def deduplicate(items: list[dict]) -> list[dict]:
    """按 title 哈希去重。"""
    seen = set()
    out = []
    for it in items:
        h = hashlib.md5(it["title"].lower().encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            out.append(it)
    return out


def build_prompt(news_items: list[dict], hormuz_json: str) -> str:
    """拼装给 DeepSeek 的中文投研摘要指令。"""
    news_text = "\n".join(
        f"[{it['source']}] {it['title']} —— {it['summary']}" for it in news_items[:30]
    )
    return f"""你是一名专注于地缘政治与原油市场的投研助理。请基于以下过去数小时的信息，输出一份**简洁、可操作**的中文简报，面向实盘投资者。

## 霍尔木兹海峡结构化状态
{hormuz_json}

## 最新地缘与原油新闻
{news_text}

---

请严格按以下结构输出，不要添加额外解释：

**一、核心事实（3~5 条）**
只列出对“美伊冲突/霍尔木兹通行/原油供应”有实质影响的事实，每条一行。

**二、地缘与供应研判（2~3 句）**
判断当前局势是在升级、僵持还是缓和，以及对霍尔木兹航运的实际威胁程度。

**三、原油价格信号（2~3 句）**
结合新闻中的措辞（如“供应中断”“fees”“closure”等），给出对油价的偏多/偏空/中性倾向，不要给出具体点位。

**四、需要盯住的下一个变量**
列出 1~2 个即将发生或可能触发的关键事件/时间节点。

语气：冷静、事实导向，不渲染恐慌，不使用“战争一触即发”等标题党措辞。"""


def call_deepseek(prompt: str) -> str:
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
    )
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=900,
    )
    return resp.choices[0].message.content.strip()


def push_to_wechat(title: str, content: str) -> None:
    r = requests.post(
        "https://www.pushplus.plus/send",
        json={
            "token": PUSHPLUS_TOKEN,
            "title": title,
            "content": content,
            "template": "markdown",
        },
        timeout=15,
    )
    r.raise_for_status()
    print(f"[PushPlus] {r.json()}")


def main():
    # 1. 并行拉取所有 RSS
    tasks = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for q in GOOGLE_NEWS_QUERIES:
            tasks.append(pool.submit(fetch_rss, google_news_url(q), f"GNews:{q}"))
        tasks.append(pool.submit(fetch_rss, OILPRICE_RSS, "OilPrice"))

        all_items = []
        for fut in as_completed(tasks):
            all_items.extend(fut.result())

    all_items = deduplicate(all_items)
    if not all_items:
        print("过去数小时没有抓到新内容，跳过本次推送。")
        return

    # 2. 拉取霍尔木兹结构化状态
    hormuz_json = fetch_hormuz_status()

    # 3. DeepSeek 研判
    prompt = build_prompt(all_items, hormuz_json)
    try:
        digest = call_deepseek(prompt)
    except Exception as exc:
        print(f"[DeepSeek 失败] {exc}")
        # 降级：直接把新闻标题推给你，不中断监控
        fallback = "DeepSeek 调用失败，以下是原始标题：\n\n" + "\n".join(
            f"- {it['title']}" for it in all_items[:15]
        )
        push_to_wechat("⚠️ 地缘/原油监控（AI 降级）", fallback)
        return

    # 4. 推送
    title = f"🛢️ 地缘/原油监控简报 · {time.strftime('%m-%d %H:%M')}"
    push_to_wechat(title, digest)
    print("推送完成。")


if __name__ == "__main__":
    main()
