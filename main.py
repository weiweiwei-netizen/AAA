import os
import json
import time
import datetime
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

TRUMP_QUERIES = [
    "\"Donald Trump\" statement",
    "\"Donald Trump\" speech",
]

OILPRICE_RSS = "https://oilprice.com/rss/main"
FED_PRESS_RSS = "https://www.federalreserve.gov/feeds/press_all.xml"
TRUMP_RSS = "https://www.trumpstruth.org/feed"
EAGLE_HORMUZ_API = "https://eagleintelmari.com/api/hormuz-status"

HOURS_LOOKBACK = 6

MARKET_SYMBOLS = [
    {"key": "brent", "name": "布伦特原油", "eastmoney_id": "112.B00Y", "unit": "美元/桶", "decimals": 2},
    {"key": "gold", "name": "伦敦金", "eastmoney_id": "101.GC00Y", "unit": "美元/盎司", "decimals": 1},
    {"key": "dxy", "name": "美元指数", "eastmoney_id": "100.UDI", "unit": "", "decimals": 2},
]
# =================================================


def format_time(ts: float | None) -> str:
    """将 UTC 时间戳转换为北京时间 (UTC+8) 的 MM-DD HH:MM 格式"""
    if not ts:
        return "未知时间"
    # GitHub Actions 跑在 UTC 时区，这里 +8 小时转为北京时间
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=8)
    return dt.strftime("%m-%d %H:%M")


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
            if published is None or published < cutoff:
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


# ---------- 行情快照 ----------

def fetch_quote(symbol: dict) -> dict | None:
    """获取实时报价：优先东方财富，失败时回退 Yahoo Finance。"""
    try:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": symbol["eastmoney_id"],
            "fields": "f43,f60,f170",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        }
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json().get("data")
        if data:
            latest_raw = data.get("f43")
            prev_raw = data.get("f60")
            pct_raw = data.get("f170")
            if latest_raw is not None and prev_raw is not None:
                pct = float(pct_raw) / 100 if pct_raw is not None else None
                price = None
                for scale in (100, 1000, 1):
                    p = float(latest_raw) / scale
                    prev = float(prev_raw) / scale
                    if prev == 0 or not (0.01 < p < 100000):
                        continue
                    implied = (p - prev) / prev * 100
                    if pct is None or abs(implied - pct) < 1.5:
                        price = p
                        break
                if price is None:
                    price = float(latest_raw) / 100
                return {
                    "name": symbol["name"],
                    "price": round(price, symbol["decimals"]),
                    "change_pct": round(pct, 2) if pct is not None else 0.0,
                    "unit": symbol["unit"],
                }
    except Exception as exc:
        print(f"[东方财富失败] {symbol['name']}: {exc}")

    yahoo_map = {"brent": "BZ=F", "gold": "GC=F", "dxy": "DX-Y.NYB"}
    yahoo_ticker = yahoo_map.get(symbol["key"])
    if not yahoo_ticker:
        return None
    try:
        y_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}"
        y_params = {"interval": "1m", "range": "1d"}
        y_headers = {"User-Agent": "Mozilla/5.0"}
        yr = requests.get(y_url, params=y_params, headers=y_headers, timeout=10)
        yr.raise_for_status()
        ydata = yr.json()
        result = ydata.get("chart", {}).get("result")
        if not result:
            print(f"[Yahoo备用失败] {symbol['name']}: 无结果")
            return None
        meta = result[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None or prev is None or prev == 0:
            print(f"[Yahoo备用失败] {symbol['name']}: 价格字段缺失")
            return None
        pct = (price - prev) / prev * 100
        return {
            "name": symbol["name"],
            "price": round(price, symbol["decimals"]),
            "change_pct": round(pct, 2),
            "unit": symbol["unit"],
        }
    except Exception as exc:
        print(f"[Yahoo备用失败] {symbol['name']}: {exc}")
        return None


def fetch_market_snapshot() -> list[dict]:
    snapshot = []
    for sym in MARKET_SYMBOLS:
        q = fetch_quote(sym)
        if q:
            snapshot.append(q)
        else:
            snapshot.append({"name": sym["name"], "price": None, "change_pct": None, "unit": sym["unit"]})
    return snapshot


def _color(pct: float | None) -> str:
    if pct is None:
        return "#888"
    return "#e74c3c" if pct > 0 else ("#27ae60" if pct < 0 else "#888")


def _sign(pct: float | None) -> str:
    if pct is None:
        return "—"
    return f"+{pct:.2f}%" if pct > 0 else f"{pct:.2f}%"


def build_market_card(snapshot: list[dict]) -> str:
    rows = []
    for q in snapshot:
        pct = q["change_pct"]
        color = _color(pct)
        if q["price"] is not None:
            price_text = f"{q['price']:,.{len(str(q['price']).split('.')[-1])}f}"
        else:
            price_text = "—"
        unit = f" {q['unit']}" if q["unit"] else ""
        rows.append(
            '<div style="display:inline-block;width:31%;vertical-align:top;margin-right:2%;'
            'background:#fff;border-radius:10px;padding:10px 8px;text-align:center;'
            'border:1px solid #eee;">'
            f'<div style="font-size:12px;color:#666;margin-bottom:6px;">{html.escape(q["name"])}</div>'
            f'<div style="font-size:16px;font-weight:bold;color:{color};margin-bottom:4px;">'
            f'{price_text}{unit}</div>'
            f'<div style="font-size:14px;font-weight:bold;color:{color};">{_sign(pct)}</div>'
            "</div>"
        )
    cards_html = "".join(rows)

    notes = []
    brent = next((q for q in snapshot if q["name"] == "布伦特原油"), None)
    gold = next((q for q in snapshot if q["name"] == "伦敦金"), None)
    dxy = next((q for q in snapshot if q["name"] == "美元指数"), None)

    if brent and brent["change_pct"] is not None and abs(brent["change_pct"]) >= 1.5:
        direction = "走强" if brent["change_pct"] > 0 else "走弱"
        notes.append(f"原油近几小时明显{direction}（{_sign(brent['change_pct'])}），地缘风险溢价仍在反复定价。")
    if gold and gold["change_pct"] is not None and abs(gold["change_pct"]) >= 1.0:
        notes.append(f"黄金出现{abs(gold['change_pct']):.1f}%级别波动，与油价及美元走势联动明显。")
    if brent and gold and brent["change_pct"] is not None and gold["change_pct"] is not None:
        if brent["change_pct"] > 0 and gold["change_pct"] < 0:
            notes.append("油涨金跌，市场更关注供应端而非避险，通胀-紧缩逻辑仍在主导。")
        elif brent["change_pct"] < 0 and gold["change_pct"] > 0:
            notes.append("油跌金涨，与“油价回落→通胀担忧缓和→金价获得喘息”的传导一致。")
    if dxy and dxy["change_pct"] is not None and abs(dxy["change_pct"]) < 0.15:
        notes.append("美元指数窄幅持稳，等待美联储决议给出方向。")

    note_text = " ".join(notes) if notes else "三个标的近几小时均未出现超出常规阈值的显著异动，以等待关键事件驱动为主。"

    return (
        '<div style="background:#f7f8fa;border-radius:12px;padding:14px 16px;margin-bottom:12px;">'
        '<div style="font-size:14px;font-weight:bold;color:#1a1a1a;'
        'border-left:3px solid #3498db;padding-left:8px;margin-bottom:10px;">'
        "📊 行情速览与异动"
        "</div>"
        f'<div style="margin-bottom:10px;">{cards_html}</div>'
        f'<div style="font-size:13px;color:#555;line-height:1.6;background:#fff;'
        f'border-radius:8px;padding:8px 10px;">{html.escape(note_text)}</div>'
        "</div>"
    )


# ---------- AI 简报 ----------

def build_prompt(news_items: list[dict], hormuz_json: str) -> str:
    geo_items = [it for it in news_items if it["category"] == "geo"]
    fed_items = [it for it in news_items if it["category"] == "fed"]
    trump_items = [it for it in news_items if it["category"] == "trump"]

    geo_text = "\n".join(
        f"[{it['source']}] (发布时间: {format_time(it['published'])}) {it['title']} —— {it['summary']}" 
        for it in geo_items[:25]
    ) or "（本时段无新的地缘/原油新闻）"

    fed_text = "\n".join(
        f"[{it['source']}] (发布时间: {format_time(it['published'])}) {it['title']} —— {it['summary']}" 
        for it in fed_items[:15]
    ) or "（本时段无新的美联储新闻）"

    trump_text = "\n".join(
        f"[{it['source']}] (发布时间: {format_time(it['published'])}) {it['title']} —— {it['summary']}" 
        for it in trump_items[:20]
    ) or "（本时段无新的特朗普公开言论或动态）"

    return f"""你是一名专注于地缘政治、原油市场与美联储政策的投研助理。请基于以下过去数小时的信息，生成一份**简洁、可操作**的中文简报，并以微信可直接渲染的 HTML 片段输出，面向实盘投资者。

## 霍尔木兹海峡结构化状态
{hormuz_json}

## 地缘与原油新闻
{geo_text}

## 美联储新闻
{fed_text}

## 特朗普动态（Truth Social 发帖 / 公开讲话 / 政策表态）
{trump_text}

---

## 一、内容要求

**核心事实（3~5 条）**
只列出对“美伊冲突/霍尔木兹通行/原油供应/美联储政策/特朗普政策”有实质影响的事实，每条一句话。

**地缘与供应研判（2~3 句）**
判断当前局势是在升级、僵持还是缓和，以及对霍尔木兹航运的实际威胁程度。

**原油价格信号（2~3 句）**
结合新闻措辞，给出对油价的偏多/偏空/中性倾向，不要给出具体点位。

**美联储动态与政策信号（2~3 句）**
聚焦利率路径、官员表态、流动性预期。若本时段无实质新信息，明确写“本时段无实质新信息”。

**特朗普动态（2~3 句）**
总结特朗普在本时段内的关键表态或动作。如果涉及能源（如甲烷法规、伊朗行动）、关税或美联储施压，需重点点出其对原油或美元的可能影响。

**需要盯住的下一个变量（1~2 条）**
列出即将发生或可能触发的关键事件/时间节点。

语气：冷静、事实导向，不渲染恐慌，不使用“战争一触即发”等标题党措辞。

## 二、时间标注要求（必须严格遵守）

1. 简报中列出的每一条核心事实、每一个特朗普动态，**必须**在其末尾附带该消息的发布时间。
2. 时间格式统一为 `MM-DD HH:MM`（注意：上面提供的时间已经是北京时间，直接用即可）。
3. 时间必须使用指定的 HTML 小字样式跟在文字后面：`<span style="font-size:12px;color:#888;background:#f0f1f3;padding:2px 6px;border-radius:4px;margin-left:6px;">10-28 14:30</span>`。
4. 如果某条研判综合了多个时间点的信息，请标注核心事件的时间。
5. 行情数据区块不需要加新闻时间。

## 三、HTML 硬性约束

1. 只输出 HTML 片段本体，不要 ```html 代码块标记，不要任何解释性文字。
2. 所有样式必须内联。禁止 <script>、<style> 标签、外部 CSS、外部字体、class 选择器。
3. 禁止 flex、grid、position: fixed/sticky、transform 等布局属性。
4. 整封简报外层用 <div style="font-family:-apple-system,'PingFang SC',sans-serif;font-size:14px;color:#333;line-height:1.6;"> 包裹。

## 四、视觉规范

- 每个内容区块一个卡片：背景 #f7f8fa，圆角 12px，padding 14px 16px，margin-bottom 12px。
- 区块标题：14px 加粗，#1a1a1a，左侧 3px 竖条强调色。
- 正文：14px，#333，行高 1.6。
- 强调色：升级/供应中断/鹰派/涉伊强硬表态 → #e74c3c；僵持/中性/常规政策 → #f39c12；缓和/无新信息 → #95a5a6。
- 区块顺序固定：① 核心事实 ② 地缘与供应 ③ 原油信号 ④ 美联储动态 ⑤ **特朗普动态** ⑥ 下一个变量。
- 美联储或特朗普若“无新信息/无言论”，收窄为浅灰单行提示。

请直接输出最终 HTML。"""


def call_deepseek(prompt: str) -> str:
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1600,
    )
    content = resp.choices[0].message.content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    return content


def push_to_wechat(title: str, html_content: str) -> None:
    r = requests.post(
        "https://www.pushplus.plus/send",
        json={"token": PUSHPLUS_TOKEN, "title": title, "content": html_content, "template": "html"},
        timeout=15,
    )
    r.raise_for_status()
    print(f"[PushPlus] {r.json()}")


def build_fallback_html(items: list[dict], market_card: str) -> str:
    rows = []
    for it in items[:20]:
        cat_map = {"geo": "地缘/原油", "fed": "美联储", "trump": "特朗普动态"}
        cat = cat_map.get(it["category"], "其他")
        pub_time = format_time(it["published"])
        rows.append(
            '<div style="padding:8px 0;border-bottom:1px solid #eee;">'
            f'<span style="font-size:12px;color:#888;">[{html.escape(cat)}]</span> '
            f'<span style="font-size:14px;color:#333;">{html.escape(it["title"])}</span>'
            f'<span style="font-size:11px;color:#999;margin-left:8px;">({pub_time})</span>'
            "</div>"
        )
    body = "".join(rows)
    return (
        '<div style="font-family:-apple-system,\'PingFang SC\',sans-serif;line-height:1.6;">'
        '<div style="background:#fff3cd;border-left:3px solid #f39c12;padding:10px 14px;'
        'border-radius:8px;margin-bottom:12px;font-size:13px;color:#856404;">'
        "AI 研判暂不可用，以下为行情快照与原始新闻标题。"
        "</div>"
        f"{market_card}"
        f'<div style="background:#f7f8fa;border-radius:12px;padding:14px 16px;">{body}</div>'
        "</div>"
    )


def main():
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = []
        for q in GEO_QUERIES:
            futures.append(pool.submit(fetch_rss, google_news_url(q), f"GNews:{q}", "geo"))
        for q in FED_QUERIES:
            futures.append(pool.submit(fetch_rss, google_news_url(q), f"GNews:{q}", "fed"))
        for q in TRUMP_QUERIES:
            futures.append(pool.submit(fetch_rss, google_news_url(q), f"GNews:{q}", "trump"))
        
        futures.append(pool.submit(fetch_rss, OILPRICE_RSS, "OilPrice", "geo"))
        futures.append(pool.submit(fetch_rss, FED_PRESS_RSS, "FedPress", "fed"))
        futures.append(pool.submit(fetch_rss, TRUMP_RSS, "TruthSocial", "trump"))

        all_items = []
        for fut in as_completed(futures):
            all_items.extend(fut.result())

    all_items = deduplicate(all_items)

    if not all_items:
        print("过去数小时没有抓到新内容，仅推送行情快照。")
        snapshot = fetch_market_snapshot()
        market_card = build_market_card(snapshot)
        push_to_wechat(f"📊 行情速览 · {time.strftime('%m-%d %H:%M')}", market_card)
        return

    hormuz_json = fetch_hormuz_status()
    prompt = build_prompt(all_items, hormuz_json)

    try:
        digest_html = call_deepseek(prompt)
    except Exception as exc:
        print(f"[DeepSeek 失败] {exc}")
        snapshot = fetch_market_snapshot()
        market_card = build_market_card(snapshot)
        push_to_wechat("⚠️ 监控简报（AI 降级）", build_fallback_html(all_items, market_card))
        return

    snapshot = fetch_market_snapshot()
    market_card = build_market_card(snapshot)

    final_html = digest_html + market_card
    title = f"🛢️ 地缘/原油/美联储简报 · {time.strftime('%m-%d %H:%M')}"
    push_to_wechat(title, final_html)
    print("推送完成。")


if __name__ == "__main__":
    main()
