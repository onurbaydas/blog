#!/usr/bin/env python3
"""
OB System & Security Radar Engine
Fetches, filters, heuristically scores, and categorizes tech/systems feeds.
Outputs:
  - data/radar.json (Rolling window live feed for /radar dashboard)
  - content/posts/radar/YYYY-MM-DD-sistem-ve-guvenlik-radari.md (Daily digest archive)
  - Optional Telegram alert if credentials are set in environment.
"""

import os
import sys
import re
import json
import time
import html
import hashlib
import argparse
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET

import feedparser
import requests

# Set stdout encoding to UTF-8
sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPML_FILE = os.path.join(BASE_DIR, "static", "radar", "sources.opml")
DATA_DIR = os.path.join(BASE_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "radar.json")
POSTS_RADAR_DIR = os.path.join(BASE_DIR, "content", "posts", "radar")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) OB-Radar/2.0 (+https://onurbaydas.online/radar)"

# Category definitions
CATEGORY_MAP = {
    "security": {
        "id": "security",
        "name_tr": "Güvenlik & CVE",
        "name_en": "Security & CVE",
        "badge": "SEC_INTEL",
        "icon": "🛡️"
    },
    "kernel": {
        "id": "kernel",
        "name_tr": "Linux & Çekirdek",
        "name_en": "Linux & Kernel",
        "badge": "KERNEL_SYS",
        "icon": "🐧"
    },
    "infra": {
        "id": "infra",
        "name_tr": "Altyapı & Depolama",
        "name_en": "Infra & Storage",
        "badge": "INFRA_ARCH",
        "icon": "⚙️"
    },
    "ai": {
        "id": "ai",
        "name_tr": "Yapay Zeka & GPU",
        "name_en": "AI & GPU Compute",
        "badge": "AI_COMPUTE",
        "icon": "⚡"
    },
    "community": {
        "id": "community",
        "name_tr": "Mühendislik Topluluğu",
        "name_en": "Engineering Community",
        "badge": "COMM_WIRE",
        "icon": "🌐"
    }
}

# Domain to category mapping
SOURCE_CATEGORIES = {
    "bleepingcomputer.com": "security",
    "krebsonsecurity.com": "security",
    "thehackernews.com": "security",
    "darknet.org.uk": "security",
    "allinfosecnews.com": "security",
    "phoronix.com": "kernel",
    "soylentnews.org": "kernel",
    "slashdot.org": "kernel",
    "servethehome.com": "infra",
    "yellow-bricks.com": "infra",
    "infoq.com": "infra",
    "therundown.ai": "ai",
    "tldr.tech": "ai",
    "kdnuggets.com": "ai",
    "sciencedaily.com": "ai",
    "localllama": "ai",
    "news.ycombinator.com": "community",
    "hnrss.org": "community",
    "lobste.rs": "community",
    "arstechnica.com": "community",
    "techmeme.com": "community",
    "dev.to": "community",
    "reddit.com": "community"
}

# CVE Regex
CVE_REGEX = re.compile(r"\b(CVE-\d{4}-\d{4,7})\b", re.IGNORECASE)
CRITICAL_THREAT_REGEX = re.compile(
    r"\b(0-day|zero-day|rce|remote code execution|privilege escalation|kernel panic|arbitrary code execution|unauthenticated|active exploitation)\b",
    re.IGNORECASE
)

# High value technical keywords for heuristic scoring
KEYWORDS_HIGH = [
    "cve-", "kernel", "linux", "ebpf", "bcachefs", "zfs", "nfs", "kerberos",
    "sssd", "active directory", "opa", "open policy agent", "fail-open",
    "docker", "container", "cgroup", "namespace", "gpu", "cuda", "vulnerability",
    "zero-day", "0-day", "exploit", "privilege escalation", "rce", "patch",
    "backdoor", "h100", "quantization", "nvme", "cluster"
]

KEYWORDS_MEDIUM = [
    "security", "firmware", "cpu", "amd", "intel", "arm", "memory", "leak",
    "storage", "network", "firewall", "tls", "wireguard", "dns", "systemd",
    "performance", "benchmark", "llvm", "rust", "c++", "compiler", "virtualization",
    "kvm", "qemu", "truenas", "proxmox"
]

def load_sources_from_opml(opml_path):
    """Parses sources from the OPML file."""
    sources = []
    if not os.path.exists(opml_path):
        print(f"[!] OPML file not found at: {opml_path}")
        return sources

    try:
        tree = ET.parse(opml_path)
        for outline in tree.findall(".//outline[@type='rss']"):
            xml_url = outline.get("xmlUrl")
            title = outline.get("title") or outline.get("text") or "Feed"
            html_url = outline.get("htmlUrl") or ""
            if xml_url:
                sources.append({
                    "title": title.strip(),
                    "xmlUrl": xml_url.strip(),
                    "htmlUrl": html_url.strip()
                })
    except Exception as e:
        print(f"[!] Error parsing OPML: {e}")

    return sources

def determine_category(source_title, feed_url, item_title):
    """Categorizes an item based on its source domain and keywords."""
    lookup = (source_title + " " + feed_url).lower()
    for domain, cat in SOURCE_CATEGORIES.items():
        if domain in lookup:
            return cat

    title_lower = item_title.lower()
    if any(k in title_lower for k in ["cve-", "exploit", "vulnerability", "ransomware", "malware", "security"]):
        return "security"
    if any(k in title_lower for k in ["kernel", "linux", "driver", "cpu", "mesa", "bcachefs"]):
        return "kernel"
    if any(k in title_lower for k in ["llm", "ai", "model", "gpt", "transformer", "neural", "deep learning"]):
        return "ai"
    if any(k in title_lower for k in ["server", "storage", "cloud", "datacenter", "nas", "san", "network"]):
        return "infra"

    return "community"

def clean_html_summary(raw_html, max_len=260):
    """Cleans HTML tags and strips feed noise, returning a crisp excerpt."""
    if not raw_html:
        return ""

    # Unescape first
    text = html.unescape(raw_html)
    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Normalize whitespaces
    text = re.sub(r"\s+", " ", text).strip()

    # Remove boilerplate feed signatures
    boilerplate_patterns = [
        r"The post .* appeared first on .*",
        r"Read more\.\.\.",
        r"Comments\s*$",
        r"\[&#8230;\]",
        r"\[\.\.\.\]"
    ]
    for pattern in boilerplate_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()

    # Split into sentences
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if sentences:
        chosen = ""
        for s in sentences:
            if len(chosen) + len(s) <= max_len:
                chosen = (chosen + " " + s).strip()
            else:
                if not chosen:
                    chosen = s[:max_len].rsplit(" ", 1)[0] + "..."
                break
        return chosen

    if len(text) > max_len:
        return text[:max_len].rsplit(" ", 1)[0] + "..."
    return text

def score_item(title, summary, source_title, has_cve, is_critical):
    """Calculates a heuristic importance score from 1 to 10."""
    score = 4.0  # Base score

    # Source credibility baseline
    top_sources = ["phoronix", "bleepingcomputer", "krebs", "the hacker news", "servethehome"]
    medium_sources = ["lobsters", "hacker news", "ars technica", "soylentnews"]
    
    st_lower = source_title.lower()
    if any(s in st_lower for s in top_sources):
        score += 2.0
    elif any(s in st_lower for s in medium_sources):
        score += 1.0

    # CVE & Threat detection bonus
    if has_cve:
        score += 3.0
    if is_critical:
        score += 2.0

    combined_text = (title + " " + summary).lower()

    # Technical keyword boosts
    high_hits = sum(1 for kw in KEYWORDS_HIGH if kw in combined_text)
    score += min(high_hits * 0.8, 3.0)

    med_hits = sum(1 for kw in KEYWORDS_MEDIUM if kw in combined_text)
    score += min(med_hits * 0.4, 1.5)

    # Normalize between 1 and 10
    final_score = int(round(min(max(score, 1.0), 10.0)))
    return final_score

def fetch_feed(source):
    """Fetches a single feed with timeout and headers."""
    items = []
    xml_url = source["xmlUrl"]
    source_title = source["title"]

    headers = {"User-Agent": USER_AGENT}

    try:
        resp = requests.get(xml_url, headers=headers, timeout=12)
        if resp.status_code != 200:
            return items

        feed = feedparser.parse(resp.content)
        if not feed.entries:
            return items

        for entry in feed.entries[:6]:  # Take top 6 latest items per feed
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue

            # Summary extraction
            raw_summary = entry.get("summary", "") or entry.get("description", "")
            if not raw_summary and "content" in entry and entry.content:
                raw_summary = entry.content[0].get("value", "")
            clean_summary = clean_html_summary(raw_summary)
            if not clean_summary or len(clean_summary) < 10:
                clean_summary = title

            # Published timestamp
            published_dt = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    published_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    pass
            elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                try:
                    published_dt = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    pass

            if not published_dt:
                published_dt = datetime.now(timezone.utc)

            # Detect CVE & Threat
            cve_matches = CVE_REGEX.findall(title + " " + clean_summary)
            cve_id = cve_matches[0].upper() if cve_matches else None
            is_cve = bool(cve_id)
            is_critical = bool(CRITICAL_THREAT_REGEX.search(title + " " + clean_summary))

            category = determine_category(source_title, xml_url, title)
            score = score_item(title, clean_summary, source_title, is_cve, is_critical)

            item_id = hashlib.sha256((link + title).encode("utf-8")).hexdigest()[:16]

            items.append({
                "id": item_id,
                "title": title,
                "link": link,
                "summary": clean_summary,
                "source": source_title,
                "category": category,
                "score": score,
                "is_cve": is_cve,
                "cve_id": cve_id,
                "is_critical": is_critical,
                "published_iso": published_dt.isoformat(),
                "published_ts": int(published_dt.timestamp())
            })

    except Exception as e:
        print(f"[!] Feed fetch failed ({source_title}): {e}")

    return items

def update_radar_data(new_items):
    """Maintains a rolling window of items and updates data/radar.json."""
    os.makedirs(DATA_DIR, exist_ok=True)
    existing_items = []

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                existing_items = data.get("items", [])
        except Exception as e:
            print(f"[!] Warning reading existing radar data: {e}")

    # Merge items with deduplication by link / id
    seen_ids = set()
    seen_links = set()
    merged = []

    # New items take priority
    for item in new_items + existing_items:
        clean_link = item["link"].split("?")[0].rstrip("/")
        if item["id"] in seen_ids or clean_link in seen_links:
            continue
        seen_ids.add(item["id"])
        seen_links.add(clean_link)
        merged.append(item)

    # Filter out items older than 4 days to keep feed fresh and fast
    four_days_ago = int((datetime.now(timezone.utc) - timedelta(days=4)).timestamp())
    fresh_items = [it for it in merged if it.get("published_ts", 0) >= four_days_ago]

    # Sort: CVE / Critical first, then by score desc, then by recency desc
    def sort_key(it):
        cve_priority = 100 if it.get("is_cve") else (50 if it.get("is_critical") else 0)
        return (cve_priority + it.get("score", 0), it.get("published_ts", 0))

    fresh_items.sort(key=sort_key, reverse=True)

    # Keep top 75 items
    final_items = fresh_items[:75]

    now_utc = datetime.now(timezone.utc)
    # TR time: UTC+3
    now_tr = now_utc + timedelta(hours=3)
    months_tr = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
                 "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
    human_time_tr = f"{now_tr.day} {months_tr[now_tr.month - 1]} {now_tr.year} {now_tr.strftime('%H:%M')}"

    cve_count = sum(1 for it in final_items if it.get("is_cve"))

    radar_payload = {
        "last_updated": now_tr.strftime("%Y-%m-%dT%H:%M:%S+03:00"),
        "last_updated_human": human_time_tr,
        "total_items": len(final_items),
        "cve_alerts_count": cve_count,
        "categories": CATEGORY_MAP,
        "items": final_items
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(radar_payload, f, ensure_ascii=False, indent=2)

    print(f"[+] radar.json updated: {len(final_items)} items ({cve_count} CVE alerts)")
    return radar_payload

def generate_daily_digest(radar_data):
    """Generates a daily digest post in content/posts/radar/."""
    os.makedirs(POSTS_RADAR_DIR, exist_ok=True)

    now_utc = datetime.now(timezone.utc)
    now_tr = now_utc + timedelta(hours=3)
    months_tr = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
                 "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
    date_str = now_tr.strftime("%Y-%m-%d")
    human_date = f"{now_tr.day} {months_tr[now_tr.month - 1]} {now_tr.year}"

    post_filename = f"{date_str}-sistem-ve-guvenlik-radari.md"
    post_path = os.path.join(POSTS_RADAR_DIR, post_filename)

    # Select top 12 items with high scores or CVEs
    all_items = radar_data.get("items", [])
    if not all_items:
        print("[!] No items available to generate digest.")
        return None

    # Group items
    cve_items = [it for it in all_items if it.get("is_cve") or it.get("is_critical")][:4]
    kernel_items = [it for it in all_items if it.get("category") == "kernel" and it not in cve_items][:3]
    infra_items = [it for it in all_items if it.get("category") == "infra" and it not in cve_items][:3]
    security_items = [it for it in all_items if it.get("category") == "security" and it not in cve_items][:3]
    ai_items = [it for it in all_items if it.get("category") == "ai" and it not in cve_items][:3]
    comm_items = [it for it in all_items if it.get("category") == "community" and it not in cve_items][:3]

    content = []
    content.append("---")
    content.append(f"title: \"Sistem ve Güvenlik Radarı: {human_date}\"")
    content.append(f"date: {now_tr.strftime('%Y-%m-%dT%H:%M:%S+03:00')}")
    content.append("draft: false")
    content.append("tags: [\"Radar\", \"Sistem\", \"Güvenlik\", \"Linux\", \"CVE\"]")
    content.append("categories: [\"Radar\"]")
    content.append("summary: \"Günün öne çıkan Linux çekirdeği güncellemeleri, kritik CVE/güvenlik analizleri, depolama mimarileri ve altyapı gelişmeleri.\"")
    content.append("showToc: true")
    content.append("TocOpen: false")
    content.append("---")
    content.append("")
    content.append(f"> **OPERASYONEL RAPOR // {date_str}**  ")
    content.append(f"> Bu bülten, [Canlı Sistem Radarı](/radar/) üzerinde toplanan 25+ otoriter kaynaktan derlenmiş; kural tabanlı puanlama ile filtrelenmiş günün en kritik gelişmelerini içerir.")
    content.append("")

    if cve_items:
        content.append("## 🚨 Kritik Güvenlik ve CVE Uyarıları")
        content.append("")
        for it in cve_items:
            badge = f"**[{it['cve_id']}]** " if it.get("cve_id") else "**[KRİTİK TEHDİT]** "
            content.append(f"### {badge}[{it['title']}]({it['link']})")
            content.append(f"- **Kaynak:** `{it['source']}` | **Skor:** `{it['score']}/10`")
            content.append(f"- **Özet:** {it['summary']}")
            content.append("")

    if kernel_items:
        content.append("## 🐧 Linux Çekirdeği, Donanım ve Sistem")
        content.append("")
        for it in kernel_items:
            content.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`)  \n  {it['summary']}")
        content.append("")

    if security_items:
        content.append("## 🛡️ Altyapı Güvenliği & Tehdit İstihbaratı")
        content.append("")
        for it in security_items:
            content.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`)  \n  {it['summary']}")
        content.append("")

    if infra_items:
        content.append("## ⚙️ Depolama, Ağ ve Kurumsal Mimariler")
        content.append("")
        for it in infra_items:
            content.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`)  \n  {it['summary']}")
        content.append("")

    if ai_items:
        content.append("## ⚡ Yapay Zeka Altyapısı ve GPU Hesaplama")
        content.append("")
        for it in ai_items:
            content.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`)  \n  {it['summary']}")
        content.append("")

    if comm_items:
        content.append("## 🌐 Mühendislik Topluluğu ve Tartışmalar")
        content.append("")
        for it in comm_items:
            content.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`)  \n  {it['summary']}")
        content.append("")

    content.append("---")
    content.append("📡 *Tüm canlı akışı ve anlık filtrelemeyi [onurbaydas.online/radar](/radar/) sayfasından takip edebilir, kaynak listesini [OPML formatında](/radar/sources.opml) indirebilirsiniz.*")

    full_md = "\n".join(content)
    with open(post_path, "w", encoding="utf-8") as f:
        f.write(full_md)

    print(f"[+] Daily digest post generated: {post_path}")
    return post_path

def send_telegram_alert(radar_data):
    """Sends a summary alert to Telegram if credentials are set in environment."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        print("[-] Telegram credentials not configured. Skipping Telegram notification.")
        return False

    items = radar_data.get("items", [])
    if not items:
        return False

    now_utc = datetime.now(timezone.utc)
    now_tr = now_utc + timedelta(hours=3)
    time_str = now_tr.strftime("%H:%M")

    # Pick top 5 items
    top_picks = items[:6]

    msg_lines = [
        f"🛰 <b>SİSTEM VE GÜVENLİK RADARI ({time_str})</b>",
        f"<i>Aktif Gözlem: {radar_data.get('total_items', 0)} haber | {radar_data.get('cve_alerts_count', 0)} CVE Alarmı</i>\n"
    ]

    for it in top_picks:
        emoji = "🚨" if it.get("is_cve") else ("🔥" if it.get("score", 0) >= 8 else "🔹")
        title_esc = html.escape(it["title"])
        source_esc = html.escape(it["source"])
        link = it["link"]
        score = it.get("score", 0)
        cve_tag = f" <b>[{html.escape(it['cve_id'])}]</b>" if it.get("cve_id") else ""

        line = f"{emoji}{cve_tag} <b>[{score}/10]</b> <a href=\"{link}\">{title_esc}</a> <i>({source_esc})</i>"
        msg_lines.append(line)

    msg_lines.append("\n🌐 <a href=\"https://onurbaydas.online/radar/\">Canlı Radar Terminalini Aç ➔</a>")
    full_msg = "\n".join(msg_lines)

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": full_msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            print("[+] Telegram alert sent successfully.")
            return True
        else:
            print(f"[!] Telegram API error ({r.status_code}): {r.text}")
    except Exception as e:
        print(f"[!] Telegram send exception: {e}")

    return False

def main():
    parser = argparse.ArgumentParser(description="OB Systems Radar Engine")
    parser.add_argument("--digest", action="store_true", help="Generate daily digest blog post")
    parser.add_argument("--telegram", action="store_true", help="Send alert to Telegram")
    args = parser.parse_args()

    print(f"[*] Radar run started at: {datetime.now(timezone.utc).isoformat()}")

    sources = load_sources_from_opml(OPML_FILE)
    print(f"[*] Loaded {len(sources)} sources from {OPML_FILE}")

    all_items = []
    for s in sources:
        items = fetch_feed(s)
        if items:
            all_items.extend(items)
        time.sleep(0.3)  # Gentle delay

    print(f"[*] Total fetched raw items: {len(all_items)}")
    radar_data = update_radar_data(all_items)

    if args.digest:
        generate_daily_digest(radar_data)

    if args.telegram or os.getenv("TELEGRAM_BOT_TOKEN"):
        send_telegram_alert(radar_data)

    print("[*] Radar execution completed successfully.")

if __name__ == "__main__":
    main()
