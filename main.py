import os
import json
import time
import hashlib
import html
import feedparser
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI

# ==================== 配置区 ====================
PUSHPLUS_TOKEN = os.environ["PUSHPLUS_TOKEN"]
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]

GEO_QUERIES = [
    "Iran US conflict",
    "Strait of Hormuz",
    "Iran war oil",
    "US Iran military",
]

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
                continue
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

    return f"""你是一名专注于地缘政治、原油市场与美联储政策的投研助理。请基于以下过去数小时的信息，生成一份**简洁、可操作**的中文简报，并以微信可直接渲染的 HTML 片段输出，面向实盘投资者。

## 霍尔木兹海峡结构化状态
{hormuz_json}

## 地缘与原油新闻
{geo_text}

## 美联储新闻
{fed_text}

---

## 一、内容要求（先想清楚内容，再写 HTML）

**核心事实（3~5 条）**
只列出对“美伊冲突/霍尔木兹通行/原油供应/美联储政策”有实质影响的事实，地缘与美联储可混合列出，每条一句话。

**地缘与供应研判（2~3 句）**
判断当前局势是在升级、僵持还是缓和，以及对霍尔木兹航运的实际威胁程度。

**原油价格信号（2~3 句）**
结合新闻措辞（如“供应中断”“fees”“closure”等），给出对油价的偏多/偏空/中性倾向，不要给出具体点位。

**美联储动态与政策信号（2~3 句）**
聚焦利率路径、官员表态、流动性预期，并说明其对美元及原油的间接影响。若本时段无实质新信息，明确写“本时段无实质新信息”。

**需要盯住的下一个变量（1~2 条）**
列出即将发生或可能触发的关键事件/时间节点，地缘与美联储均可。

语气：冷静、事实导向，不渲染恐慌，不使用“战争一触即发”等标题党措辞。

## 二、HTML 硬性约束（必须严格遵守）

1. 只输出 HTML 片段本体，**不要** ```html 代码块标记，不要任何解释性文字。
2. 所有样式必须**内联**（写在 style="..." 中）。**禁止** <script>、<style> 标签、外部 CSS、外部字体、class 选择器。
3. **禁止** flex、grid、position: fixed/sticky、transform 等微信可能不兼容的布局属性。使用普通块级 <div> 加 margin/padding 实现间距。
4. 整封简报外层用 <div style="font-family:-apple-system,'PingFang SC',sans-serif;font-size:14px;color:#333;line-height:1.6;"> 包裹。

## 三、HTML 视觉规范

- **整体**：每个内容区块用一个卡片 <div>：背景 #f7f8fa，圆角 12px，padding 14px 16px，margin-bottom 12px。
- **区块标题**：14px 加粗，#1a1a1a，margin-bottom 8px，左侧 3px 竖条强调色。
- **正文**：14px，#333，行高 1.6。
- **辅助/无新信息提示**：12px，#888，浅灰背景 #f0f1f3，圆角 8px，padding 8px 10px。
- **强调色按内容自适应**：
  - 局势升级 / 供应中断 / 鹰派超预期 → 强调色 #e74c3c（暖红）
  - 僵持 / 混合信号 / 中性 → 强调色 #f39c12（琥珀）
  - 缓和 / 无实质新信息 → 强调色 #95a5a6（灰）
  每个卡片根据其内容自行选择合适的强调色，同一封简报内可混用。
- **核心事实**：每条事实单独一行，行首用一个带强调色的小方块符号（用 <span style="display:inline-block;width:6px;height:6px;background:#e74c3c;border-radius:2px;margin-right:8px;vertical-align:middle;"></span> 表示，颜色随事实性质变化）。
- **五个区块顺序固定**：① 核心事实 ② 地缘与供应 ③ 原油信号 ④ 美联储动态 ⑤ 下一个变量。
- 美联储若“无实质新信息”，该区块内容用一个浅灰单行提示块呈现，不要展开成大段文字，区块标题强调色用 #95a5a6。

请直接输出最终 HTML。"""


def call_deepseek(prompt: str) -> str:
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
    )
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1600,
    )
    content = resp.choices[0].message.content.strip()
    # 容错：若模型仍包了代码块标记，剥掉
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    return content


def push_to_wechat(title: str, html_content: str) -> None:
    r = requests.post(
        "https://www.pushplus.plus/send",
        json={
            "token": PUSHPLUS_TOKEN,
            "title": title,
            "content": html_content,
            "template": "html",
        },
        timeout=15,
    )
    r.raise_for_status()
    print(f"[PushPlus] {r.json()}")


def build_fallback_html(items: list[dict]) -> str:
    """DeepSeek 失败时的降级推送：简单 HTML 列表，避免 html 模板下显示乱码。"""
    rows = []
    for it in items[:20]:
        cat = "地缘/原油" if it["category"] == "geo" else "美联储"
        rows.append(
            '<div style="padding:8px 0;border-bottom:1px solid #eee;">'
            f'<span style="font-size:12px;color:#888;">[{html.escape(cat)}]</span> '
            f'<span style="font-size:14px;color:#333;">{html.escape(it["title"])}</span>'
            "</div>"
        )
    body = "".join(rows)
    return (
        '<div style="font-family:-apple-system,\'PingFang SC\',sans-serif;line-height:1.6;">'
        '<div style="background:#fff3cd;border-left:3px solid #f39c12;padding:10px 14px;'
        'border-radius:8px;margin-bottom:12px;font-size:13px;color:#856404;">'
        "AI 研判暂不可用，以下为原始新闻标题，供快速浏览。"
        "</div>"
        f'<div style="background:#f7f8fa;border-radius:12px;padding:14px 16px;">{body}</div>'
        "</div>"
    )


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
        digest_html = call_deepseek(prompt)
    except Exception as exc:
        print(f"[DeepSeek 失败] {exc}")
        fallback_html = build_fallback_html(all_items)
        push_to_wechat("⚠️ 地缘/原油/美联储监控（AI 降级）", fallback_html)
        return

    title = f"🛢️ 地缘/原油/美联储简报 · {time.strftime('%m-%d %H:%M')}"
    push_to_wechat(title, digest_html)
    print("推送完成。")


if __name__ == "__main__":
    main()
