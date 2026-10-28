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

# 地缘/原油相关关键词
GEO_QUERIES = [
    "Iran US conflict",
    "Strait of Hormuz",
    "Iran war oil",
    "US Iran military",
]

# 美联储相关关键词
FED_QUERIES = [
    "Federal Reserve",
    "FOMC rate decision",
]

OILPRICE_RSS = "https://oilprice.com/rss/main"
FED_PRESS_RSS = "https://www.federalreserve.gov/feeds/press_all.xml"
EAGLE_HORMUZ_API = "https://eagleintelmari.com/api/hormuz-status"

HOURS_LOOKBACK = 6
# =================================================


def google_news_url(query: str) -> str:
    from urllib.parse import quote
    q = quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def fetch_rss(url: str, source_name: str, category: str) -> list[dict]:
    """抓取单个 RSS，返回带 category 的标准化条目。"""
    try:
        feed = feedparser.parse(url)
        entries = []
        cutoff = time.time() - HOURS_LOOKBACK * 3600
        for e in feed.entries[:20]:
            published = None
            if hasattr(e, "published_parsed") and e.published_parsed:
                published = time.mktime(e.published_parsed)
            elif hasattr(e, "updated_parsed") and e.updated_parsed:
                published = time.mktime(e.updated_parsed)
            if published is None:
                continue  # 无时间字段，跳过，避免重复推送旧闻
            if published < cutoff:
                continue
            entries.append({
                "source": source_name,
                "category": category,
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
    try:
        r = requests.get(EAGLE_HORMUZ_API, timeout=15)
        r.raise_for_status()
        return json.dumps(r.json(), ensure_ascii=False)
    except Exception as exc:
        return f"霍尔木兹状态 API 暂不可达：{exc}"


def deduplicate(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items:
        h = hashlib.md5(it["title"].lower().encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            out.append(it)
    return out


def build_prompt(news_items: list[dict], hormuz_json: str) -> str:
    geo_items = [it for it in news_items if it["category"] == "geo"]
    fed_items = [it for it in news_items if it["category"] == "fed"]

    geo_text = "\n".join(
        f"[{it['source']}] {it['title']} —— {it['summary']}" for it in geo_items[:25]
    ) or "（本时段无新的地缘/原油新闻）"

    fed_text = "\n".join(
        f"[{it['source']}] {it['title']} —— {it['summary']}" for it in fed_items[:15]
    ) or "（本时段无新的美联储新闻）"

    return f"""你是一名专注于地缘政治、原油市场与美联储政策的投研助理。请基于以下过去数小时的信息，输出一份**简洁、可操作**的中文简报，面向实盘投资者。

## 霍尔木兹海峡结构化状态
{hormuz_json}

## 地缘与原油新闻
{geo_text}

## 美联储新闻
{fed_text}

---

请严格按以下结构输出，不要添加额外解释：

**一、核心事实（3~5 条）**
只列出对“美伊冲突/霍尔木兹通行/原油供应/美联储政策”有实质影响的事实，每条一行，地缘与美联储事实可混合列出。

**二、地缘与供应研判（2~3 句）**
判断当前局势是在升级、僵持还是缓和，以及对霍尔木兹航运的实际威胁程度。

**三、原油价格信号（2~3 句）**
结合新闻中的措辞（如“供应中断”“fees”“closure”等），给出对油价的偏多/偏空/中性倾向，不要给出具体点位。

**四、美联储动态与政策信号（2~3 句）**
聚焦利率路径、官员表态、流动性预期，并说明其对美元及原油的间接影响。若本时段无实质新信息，明确写“本时段无实质新信息”。

**五、需要盯住的下一个变量**
列出 1~2 个即将发生或可能触发的关键事件/时间节点，地缘与美联储均可。

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
        max_tokens=1000,
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
    tasks = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for q in GEO_QUERIES:
            tasks.append(pool.submit(fetch_rss, google_news_url(q), f"GNews:{q}", "geo"))
        for q in FED_QUERIES:
            tasks.append(pool.submit(fetch_rss, google_news_url(q), f"GNews:{q}", "fed"))
        tasks.append(pool.submit(fetch_rss, OILPRICE_RSS, "OilPrice", "geo"))
        tasks.append(pool.submit(fetch_rss, FED_PRESS_RSS, "FedPress", "fed"))

        all_items = []
        for fut in as_completed(tasks):
            all_items.extend(fut.result())

    all_items = deduplicate(all_items)
    if not all_items:
        print("过去数小时没有抓到新内容，跳过本次推送。")
        return

    hormuz_json = fetch_hormuz_status()

    prompt = build_prompt(all_items, hormuz_json)
    try:
        digest = call_deepseek(prompt)
    except Exception as exc:
        print(f"[DeepSeek 失败] {exc}")
        fallback = "DeepSeek 调用失败，以下是原始标题：\n\n" + "\n".join(
            f"- [{it['category']}] {it['title']}" for it in all_items[:20]
        )
        push_to_wechat("⚠️ 地缘/原油/美联储监控（AI 降级）", fallback)
        return

    title = f"🛢️ 地缘/原油/美联储简报 · {time.strftime('%m-%d %H:%M')}"
    push_to_wechat(title, digest)
    print("推送完成。")


if __name__ == "__main__":
    main()
