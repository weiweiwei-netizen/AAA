import os
import json
import time
import datetime
import hashlib
import html
import re
import feedparser
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
from cme_fedwatch import get_probabilities

# ==================== 配置区 ====================
PUSHPLUS_TOKEN = os.environ["PUSHPLUS_TOKEN"]
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]

# 群组编码（一对多推送）
PUSHPLUS_TOPIC = "88888888" 

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

MARKET_SYMBOLS = [
    {"key": "brent", "name": "布伦特原油", "eastmoney_id": "112.B00Y", "unit": "美元/桶", "decimals": 2},
    {"key": "gold", "name": "伦敦金", "eastmoney_id": "101.GC00Y", "unit": "美元/盎司", "decimals": 1},
    {"key": "dxy", "name": "美元指数", "eastmoney_id": "100.UDI", "unit": "", "decimals": 2},
]
# =================================================


def format_time(ts: float | None) -> str:
    if not ts:
        return "未知时间"
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=8)
    return dt.strftime("%m-%d %H:%M")


def get_beijing_time_now() -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    return dt.strftime("%m-%d %H:%M")


def google_news_url(query: str) -> str:
    from urllib.parse import quote
    q = quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def fetch_rss(url: str, source_name: str, category: str, lookback_hours: float) -> list[dict]:
    try:
        feed = feedparser.parse(url)
        entries = []
        cutoff = time.time() - lookback_hours * 3600
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


# ---------- FedWatch 概率卡片 ----------

def fetch_fedwatch_card() -> tuple[str, str]:
    try:
        all_data = get_probabilities()
        if not all_data or "meetings" not in all_data:
            return "", "（本时段未获取到 FedWatch 利率预期数据）"

        meetings = all_data["meetings"]
        today = datetime.datetime.now(datetime.timezone.utc).date()

        future_meetings = []
        for m in meetings:
            try:
                meeting_date = datetime.datetime.strptime(m["date"], "%Y-%m-%d").date()
                if meeting_date >= today:
                    future_meetings.append(m)
            except (ValueError, KeyError):
                continue
        
        if not future_meetings:
            return "", "（FedWatch 数据中暂无未来的 FOMC 会议）"
        
        future_meetings.sort(key=lambda x: x["date"])
        m = future_meetings[0]
        
        meeting_date_str = m["date"]
        probs = m["probabilities"]
        current_target = all_data.get("current_target", "未知")
        trade_date = all_data.get("trade_date", "未知")

        prob_lines = []
        for rate_range, prob in probs.items():
            if prob > 0:
                prob_lines.append(f"{rate_range}: {prob:.1f}%")
        probs_text = "、".join(prob_lines)

        ai_summary = (
            f"下一次 FOMC 会议日期：{meeting_date_str}（数据基准日：{trade_date}），"
            f"当前基准利率区间：{current_target}。"
            f"市场隐含概率分布为：{probs_text}。"
        )

        rows = []
        for rate_range, prob in probs.items():
            if prob <= 0:
                continue
            color = "#e74c3c" if prob >= 50 else "#888"
            rows.append(
                '<div style="display:inline-block;width:48%;vertical-align:top;margin-bottom:6px;">'
                f'<span style="font-size:13px;color:#333;">{html.escape(rate_range)}</span>'
                f'<span style="font-size:13px;font-weight:bold;color:{color};float:right;margin-right:4%;">{prob:.1f}%</span>'
                "</div>"
            )
        cards_html = "".join(rows)

        fedwatch_html = (
            '<div style="clear:both; margin-top:16px; background:#f7f8fa;border-radius:12px;padding:14px 16px;margin-bottom:12px;">'
            '<div style="font-size:14px;font-weight:bold;color:#1a1a1a;'
            'border-left:3px solid #9b59b6;padding-left:8px;margin-bottom:10px;">'
            f"🎯 CME FedWatch · 下次会议 {meeting_date_str}"
            "</div>"
            f'<div style="font-size:12px;color:#666;margin-bottom:8px;">当前基准利率区间：{current_target} · 数据基准日：{trade_date}</div>'
            f'<div style="background:#fff;border-radius:8px;padding:10px;">{cards_html}</div>'
            "</div>"
        )
        return fedwatch_html, ai_summary
    except Exception as exc:
        print(f"[FedWatch 抓取失败] {exc}")
        return "", "（本时段 FedWatch 数据接口异常）"


# ---------- 行情快照 ----------

def fetch_quote(symbol: dict) -> dict | None:
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
        '<div style="clear:both; margin-top:16px; background:#f7f8fa;border-radius:12px;padding:14px 16px;margin-bottom:12px;">'
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

def build_prompt(news_items: list[dict], hormuz_json: str, fedwatch_summary: str, is_morning_summary: bool) -> str:
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

    morning_note = "**注意：这是早间汇总版，请把核心事实提炼得更全面一些，覆盖昨夜到今晨的重要变化。**" if is_morning_summary else ""

    return f"""你是一名专注于地缘政治、原油市场与美联储政策的投研助理。请基于以下过去数小时的信息，生成一份**简洁、可操作**的中文简报，并以微信可直接渲染的 HTML 片段输出，面向实盘投资者。
{morning_note}

## 霍尔木兹海峡结构化状态
{hormuz_json}

## CME FedWatch 利率预期数据
{fedwatch_summary}

## 地缘与原油新闻
{geo_text}

## 美联储新闻
{fed_text}

## 特朗普动态（Truth Social 发帖 / 公开讲话 / 政策表态）
{trump_text}

---

## 一、内容要求

**核心事实（3~5 条）**
只列出对“美伊冲突/霍尔木兹通行/原油供应/美联储政策/特朗普政策”有实质影响的事实，每条独立成行。

**地缘与供应研判（2~3 句）**
判断当前局势是在升级、僵持还是缓和，以及对霍尔木兹航运的实际威胁程度。

**原油价格信号（2~3 句）**
结合新闻措辞，给出对油价的偏多/偏空/中性倾向，不要给出具体点位。

**美联储动态与政策信号（2~3 句）**
聚焦利率路径、官员表态、流动性预期。**请务必结合上方提供的 CME FedWatch 概率数据**，解读市场对下次会议加降息幅度的真实定价，并说明其对美元及原油的间接影响。若本时段无实质新信息，明确写“本时段无实质新信息”。

**特朗普动态（2~3 句）**
总结特朗普在本时段内的关键表态或动作。如果涉及能源（如甲烷法规、伊朗行动）、关税或美联储施压，需重点点出其对原油或美元的可能影响。多条动作请独立成行。

**需要盯住的下一个变量（1~2 条）**
列出即将发生或可能触发的关键事件/时间节点。

语气：冷静、事实导向，不渲染恐慌，不使用“战争一触即发”等标题党措辞。

## 二、列表与排版符号要求（必须严格遵守）

1. 在“核心事实”板块，以及“特朗普动态”板块中，如果列出了多条事实或动作，**每一条必须独立成行**，且**不能**使用原生的 `<ul>`、`<li>`、`-` 或 `•`。
2. 每一条事实或动作的开头，**必须**强制使用下面这段确切的 HTML 代码（直接复制这段代码作为前缀，只修改背景色）：
   `<span style="display:inline-block;width:6px;height:6px;border-radius:2px;margin-right:8px;vertical-align:middle;background:#e74c3c;"></span>`
3. 方块颜色自适应：
   - 升级/中断/鹰派/强硬表态：将上方代码的 background 改为 `#e74c3c`（红色）
   - 僵持/中性/常规动作：将上方代码的 background 改为 `#f39c12`（琥珀色）
   - 缓和/次要消息：将上方代码的 background 改为 `#95a5a6`（灰色）

## 三、时间标注要求

1. 简报中列出的每一条核心事实、每一个特朗普动态，**必须**在结尾附带发布时间。
2. 时间格式统一为 `MM-DD HH:MM`（直接使用提供的时间）。
3. 必须使用下方指定的 HTML 小字样式跟在文字后面：`<span style="font-size:12px;color:#888;background:#f0f1f3;padding:2px 6px;border-radius:4px;margin-left:6px;">10-28 14:30</span>`。

## 四、HTML 硬性约束（防崩溃核心）

1. 只输出 HTML 片段本体，不要 ```html 代码块标记，不要任何解释性文字。
2. 所有样式必须内联。禁止 <script>、<style> 标签、外部 CSS、外部字体、class 选择器。
3. 禁止 flex、grid、position: fixed/sticky、transform 等布局属性。
4. **每一个 `<div>` 和 `<span>` 标签都必须有严格的闭合 `</div>` 和 `</span>`，绝不能出现漏闭合情况。** 这非常重要，否则会导致微信排版崩溃。
5. 整封简报外层用 `<div style="font-family:-apple-system,'PingFang SC',sans-serif;font-size:14px;color:#333;line-height:1.6;">` 包裹。

## 五、视觉规范

- 每个内容区块一个卡片：背景 #f7f8fa，圆角 12px，padding 14px 16px，margin-bottom 12px。
- 区块标题：14px 加粗，#1a1a1a，左侧 3px 竖条强调色。
- 正文：14px，#333，行高 1.6。
- 强调色：升级/中断/鹰派/涉伊强硬 → #e74c3c；僵持/中性/常规 → #f39c12；缓和/无新信息 → #95a5a6。
- 区块顺序固定：① 核心事实 ② 地缘与供应 ③ 原油信号 ④ 美联储动态 ⑤ **特朗普动态** ⑥ 下一个变量。
- 美联储或特朗普若“无新信息/无言论”，收窄为浅灰单行提示。

请直接输出最终 HTML。"""


def auto_fix_html(html_str: str) -> str:
    open_div = html_str.count('<div')
    close_div = html_str.count('</div>')
    if open_div > close_div:
        html_str += '</div>' * (open_div - close_div)
    
    open_span = html_str.count('<span')
    close_span = html_str.count('</span>')
    if open_span > close_span:
        html_str += '</span>' * (open_span - close_span)
        
    return html_str


def call_deepseek(prompt: str) -> str:
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=2500,
    )
    content = resp.choices[0].message.content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    
    content = re.sub(r'\n+', '\n', content)
    content = auto_fix_html(content)
    return content


def push_to_wechat(title: str, html_content: str) -> None:
    safe_html = f'<div style="overflow-x:hidden; width:100%;">{html_content}</div>'
    
    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": safe_html,
        "template": "html"
    }
    
    # 群组推送护甲
    if PUSHPLUS_TOPIC:
        payload["topic"] = PUSHPLUS_TOPIC
        print(f"[PushPlus] 正在推送到群组: {PUSHPLUS_TOPIC}")
    else:
        print("[PushPlus] 正在推送给个人（未配置 topic）")

    r = requests.post(
        "https://www.pushplus.plus/send",
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    print(f"[PushPlus 响应] {r.json()}")


def build_fallback_html(items: list[dict], market_card: str, fedwatch_card: str) -> str:
    rows = []
    for it in items[:20]:
        cat_map = {"geo": "地缘/原油", "fed": "美联储", "trump": "特朗普动态"}
        cat = cat_map.get(it["category"], "其他")
        pub_time = format_time(it["published"])
        rows.append(
            '<div style="padding:8px 0;border-bottom:1px solid #eee;">'
            '<span style="display:inline-block;width:6px;height:6px;background:#e74c3c;border-radius:2px;margin-right:8px;vertical-align:middle;"></span>'
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
        "AI 研判暂不可用，以下为行情快照、FedWatch 与原始新闻标题。"
        "</div>"
        f"{market_card}"
        f"{fedwatch_card}"
        f'<div style="background:#f7f8fa;border-radius:12px;padding:14px 16px;">{body}</div>'
        "</div>"
    )


def main():
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    current_hour = now.hour
    
    # 🌟 1. 夜间静默期（北京时间 0:00 - 7:59）
    if 0 <= current_hour < 8:
        print(f"[{get_beijing_time_now()}] 当前处于夜间静默期，跳过本次推送。")
        return
        
    # 🌟 2. 设定动态抓取时间窗口（早间汇总 vs 常规）
    if 8 <= current_hour < 10:
        lookback = 8.5  # 覆盖从昨晚 23:30 到今早 8:00 的新闻
        is_morning_summary = True
        print(f"[{get_beijing_time_now()}] 处于早间汇总模式，抓取过去 {lookback} 小时的新闻。")
    else:
        lookback = 2.5
        is_morning_summary = False
        print(f"[{get_beijing_time_now()}] 常规推送模式，抓取过去 {lookback} 小时的新闻。")

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = []
        for q in GEO_QUERIES:
            futures.append(pool.submit(fetch_rss, google_news_url(q), f"GNews:{q}", "geo", lookback))
        for q in FED_QUERIES:
            futures.append(pool.submit(fetch_rss, google_news_url(q), f"GNews:{q}", "fed", lookback))
        for q in TRUMP_QUERIES:
            futures.append(pool.submit(fetch_rss, google_news_url(q), f"GNews:{q}", "trump", lookback))
        
        futures.append(pool.submit(fetch_rss, OILPRICE_RSS, "OilPrice", "geo", lookback))
        futures.append(pool.submit(fetch_rss, FED_PRESS_RSS, "FedPress", "fed", lookback))
        futures.append(pool.submit(fetch_rss, TRUMP_RSS, "TruthSocial", "trump", lookback))

        fedwatch_future = pool.submit(fetch_fedwatch_card)

        all_items = []
        for fut in as_completed(futures):
            all_items.extend(fut.result())

    all_items = deduplicate(all_items)
    fedwatch_card, fedwatch_summary = fedwatch_future.result()

    if not all_items:
        print("过去数小时没有抓到新内容，仅推送行情快照与FedWatch。")
        snapshot = fetch_market_snapshot()
        market_card = build_market_card(snapshot)
        final_html = market_card + fedwatch_card
        push_title = f"☀️ 早间汇总（无新消息） · {get_beijing_time_now()}" if is_morning_summary else f"📊 行情与FedWatch速览 · {get_beijing_time_now()}"
        push_to_wechat(push_title, final_html)
        return

    hormuz_json = fetch_hormuz_status()
    prompt = build_prompt(all_items, hormuz_json, fedwatch_summary, is_morning_summary)

    try:
        digest_html = call_deepseek(prompt)
    except Exception as exc:
        print(f"[DeepSeek 失败] {exc}")
        snapshot = fetch_market_snapshot()
        market_card = build_market_card(snapshot)
        fallback_title = "⚠️ 早间汇总（AI降级）" if is_morning_summary else "⚠️ 监控简报（AI降级）"
        push_to_wechat(fallback_title, build_fallback_html(all_items, market_card, fedwatch_card))
        return

    snapshot = fetch_market_snapshot()
    market_card = build_market_card(snapshot)

    # 组装最终推送
    final_html = digest_html + market_card + fedwatch_card
    
    if is_morning_summary:
        title = f"☀️ 地缘/原油/美联储早间汇总 · {get_beijing_time_now()}"
    else:
        title = f"🛢️ 地缘/原油/美联储简报 · {get_beijing_time_now()}"
        
    push_to_wechat(title, final_html)
    print("推送完成。")


if __name__ == "__main__":
    main()
