#!/usr/bin/env python3
"""
OB System & Security Radar Engine (Advanced Edition)
Features:
  1. 25+ Authoritative RSS/Atom feeds (Linux, Security, Infra, AI, Community)
  2. Live Kernel.org Watchdog (Mainline, Stable, LTS release tracking)
  3. GitHub Systems Trending (eBPF, Linux kernel, storage, low-level tooling)
  4. Heuristic Scoring (1-10) with Zero LLM Cost
  5. Automated CVE & Zero-Day Detection with official NIST NVD / MITRE deep-links
  6. Dual-link architecture: Direct Source + Engineering Community Discussion (HN / Lobsters)
  7. Internal Knowledge Cross-Referencing (Auto-linking to Onur Baydas's deep-dive post-mortems)
  8. Rolling window JSON data (data/radar.json) for /radar/ dashboard
  9. Standup Triage filter support
  10. Automated daily markdown digest generation (content/posts/radar/...)
  11. Optional Telegram notification broadcast
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

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPML_FILE = os.path.join(BASE_DIR, "static", "radar", "sources.opml")
DATA_DIR = os.path.join(BASE_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "radar.json")
POSTS_RADAR_DIR = os.path.join(BASE_DIR, "content", "posts", "radar")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) OB-Radar/2.5 (+https://onurbaydas.online/radar)"

# Category Definitions
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
    "github": {
        "id": "github",
        "name_tr": "Açık Kaynak Sistemler",
        "name_en": "Open Source Systems",
        "badge": "GIT_SYSTEMS",
        "icon": "🐙"
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

# Internal Articles for Cross-Referencing
INTERNAL_ARTICLES = [
    {
        "id": "docker-authz-opa",
        "title_tr": "OPA ile Docker Authorization: Sessiz Fail-Open ve Canlı Ortamdan Dersler",
        "title_en": "Docker Authorization with OPA: Silent Fail-Open and Real-World Lessons",
        "url_tr": "/posts/docker-authz-opa/",
        "url_en": "/en/posts/docker-authz-opa/",
        "badge_tr": "OB_ANALİZİ // DOCKER_AUTHZ_OPA",
        "badge_en": "OB_ANALYSIS // DOCKER_AUTHZ_OPA",
        "keywords": [
            "opa", "open policy agent", "rego", "docker authorization",
            "docker authz", "fail-open", "cve-2024-41110", "docker daemon",
            "authorization plugin"
        ]
    },
    {
        "id": "nfs-uid-mismatch",
        "title_tr": "Bir Kullanıcı, İki UID: Domain'e Bağlı TrueNAS'ta NFSv4, Kerberos ve Docker",
        "title_en": "One User, Two UIDs: NFSv4, Kerberos, and Docker on TrueNAS",
        "url_tr": "/posts/nfs-uid-mismatch/",
        "url_en": "/en/posts/nfs-uid-mismatch/",
        "badge_tr": "OB_ANALİZİ // NFSv4_KERBEROS_SSSD",
        "badge_en": "OB_ANALYSIS // NFSv4_KERBEROS_SSSD",
        "keywords": [
            "truenas", "nfs", "nfsv4", "kerberos", "sssd", "active directory",
            "uid", "gid", "nobody:nogroup", "pam_mount", "zfs dataset",
            "idmapd", "nfs kerberos"
        ]
    }
]

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

def fetch_kernel_watchdog():
    """Fetches real-time Linux kernel releases from kernel.org."""
    watchdog = {
        "status": "ONLINE",
        "mainline": "N/A",
        "stable": "N/A",
        "longterm": "N/A",
        "checked_at": datetime.now(timezone.utc).strftime("%H:%M UTC")
    }
    try:
        r = requests.get("https://www.kernel.org/releases.json", headers={"User-Agent": USER_AGENT}, timeout=8)
        if r.status_code == 200:
            data = r.json()
            releases = data.get("releases", [])
            for rel in releases:
                moniker = rel.get("moniker", "").lower()
                version = rel.get("version", "")
                if moniker == "mainline" and watchdog["mainline"] == "N/A":
                    watchdog["mainline"] = version
                elif moniker == "stable" and watchdog["stable"] == "N/A":
                    watchdog["stable"] = version
                elif moniker == "longterm" and watchdog["longterm"] == "N/A":
                    watchdog["longterm"] = version
            print(f"[+] Kernel.org Watchdog: Mainline {watchdog['mainline']} | Stable {watchdog['stable']} | LTS {watchdog['longterm']}")
    except Exception as e:
        print(f"[!] Kernel.org Watchdog warning: {e}")

    return watchdog

def fetch_github_systems_trending():
    """Fetches trending/hot open source systems repositories."""
    items = []
    headers = {"User-Agent": USER_AGENT}
    try:
        # Query active open-source systems repositories in eBPF, kernel tools, low-level Rust/C
        url = "https://api.github.com/search/repositories?q=topic:ebpf+stars:>150&sort=updated&order=desc&per_page=4"
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            repos = r.json().get("items", [])
            now_dt = datetime.now(timezone.utc)
            for repo in repos:
                name = repo.get("full_name", "")
                desc = repo.get("description", "") or "High performance open-source systems tool."
                html_url = repo.get("html_url", "")
                stars = repo.get("stargazers_count", 0)
                lang = repo.get("language", "Systems") or "Systems"

                item_id = hashlib.sha256(("github_" + name).encode("utf-8")).hexdigest()[:16]
                summary = f"[{lang} ★{stars}] {clean_html_summary(desc, max_len=240)}"

                items.append({
                    "id": item_id,
                    "title": f"{name}: {clean_html_summary(desc, max_len=80)}",
                    "link": html_url,
                    "summary": summary,
                    "source": "GitHub Trending",
                    "category": "github",
                    "score": 8,
                    "is_cve": False,
                    "cve_id": None,
                    "is_critical": False,
                    "comments_url": f"{html_url}/issues",
                    "related_post": match_internal_article(name + " " + desc),
                    "published_iso": now_dt.isoformat(),
                    "published_ts": int(now_dt.timestamp())
                })
            print(f"[+] GitHub Trending: Fetched {len(items)} systems repositories")
    except Exception as e:
        print(f"[!] GitHub Trending warning: {e}")

    return items

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

    text = html.unescape(raw_html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    boilerplate_patterns = [
        r"The post .* appeared first on .*",
        r"Read more\.\.\.",
        r"Comments\s*$",
        r"\[&#8230;\]",
        r"\[\.\.\.\]"
    ]
    for pattern in boilerplate_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()

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

def match_internal_article(text):
    """Checks if incoming text relates to Onur Baydas's published technical articles."""
    text_lower = text.lower()
    for art in INTERNAL_ARTICLES:
        for kw in art["keywords"]:
            # Word boundary matching
            if re.search(r"\b" + re.escape(kw) + r"\b", text_lower):
                return art
    return None

def score_item(title, summary, source_title, has_cve, is_critical):
    """Calculates a heuristic importance score from 1 to 10."""
    score = 4.0

    top_sources = ["phoronix", "bleepingcomputer", "krebs", "the hacker news", "servethehome"]
    medium_sources = ["lobsters", "hacker news", "ars technica", "soylentnews"]

    st_lower = source_title.lower()
    if any(s in st_lower for s in top_sources):
        score += 2.0
    elif any(s in st_lower for s in medium_sources):
        score += 1.0

    if has_cve:
        score += 3.0
    if is_critical:
        score += 2.0

    combined_text = (title + " " + summary).lower()

    high_hits = sum(1 for kw in KEYWORDS_HIGH if kw in combined_text)
    score += min(high_hits * 0.8, 3.0)

    med_hits = sum(1 for kw in KEYWORDS_MEDIUM if kw in combined_text)
    score += min(med_hits * 0.4, 1.5)

    final_score = int(round(min(max(score, 1.0), 10.0)))
    return final_score

def fetch_feed(source):
    """Fetches a single feed with timeout, community comments link, and headers."""
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

        for entry in feed.entries[:6]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue

            raw_summary = entry.get("summary", "") or entry.get("description", "")
            if not raw_summary and "content" in entry and entry.content:
                raw_summary = entry.content[0].get("value", "")
            clean_summary = clean_html_summary(raw_summary)
            if not clean_summary or len(clean_summary) < 10:
                clean_summary = title

            # Detect Community Discussion Link (HN / Lobsters / Reddit)
            comments_url = getattr(entry, "comments", None)
            if not comments_url:
                # For Reddit RSS, link is usually the thread itself
                if "reddit.com" in xml_url and "/comments/" in link:
                    comments_url = link

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

            cve_matches = CVE_REGEX.findall(title + " " + clean_summary)
            cve_id = cve_matches[0].upper() if cve_matches else None
            is_cve = bool(cve_id)
            is_critical = bool(CRITICAL_THREAT_REGEX.search(title + " " + clean_summary))

            category = determine_category(source_title, xml_url, title)
            score = score_item(title, clean_summary, source_title, is_cve, is_critical)

            # Check for internal article cross-reference
            related_post = match_internal_article(title + " " + clean_summary)

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
                "cve_url": f"https://nvd.nist.gov/vuln/detail/{cve_id}" if is_cve else None,
                "is_critical": is_critical,
                "comments_url": comments_url,
                "related_post": related_post,
                "published_iso": published_dt.isoformat(),
                "published_ts": int(published_dt.timestamp())
            })

    except Exception as e:
        print(f"[!] Feed fetch failed ({source_title}): {e}")

    return items

def update_radar_data(new_items, kernel_status):
    """Maintains rolling window of items and updates data/radar.json."""
    os.makedirs(DATA_DIR, exist_ok=True)
    existing_items = []

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                existing_items = data.get("items", [])
        except Exception as e:
            print(f"[!] Warning reading existing radar data: {e}")

    seen_ids = set()
    seen_links = set()
    merged = []

    for item in new_items + existing_items:
        clean_link = item["link"].split("?")[0].rstrip("/")
        if item["id"] in seen_ids or clean_link in seen_links:
            continue
        seen_ids.add(item["id"])
        seen_links.add(clean_link)
        merged.append(item)

    four_days_ago = int((datetime.now(timezone.utc) - timedelta(days=4)).timestamp())
    fresh_items = [it for it in merged if it.get("published_ts", 0) >= four_days_ago]

    def sort_key(it):
        cve_priority = 100 if it.get("is_cve") else (50 if it.get("is_critical") else 0)
        return (cve_priority + it.get("score", 0), it.get("published_ts", 0))

    fresh_items.sort(key=sort_key, reverse=True)
    final_items = fresh_items[:80]

    now_utc = datetime.now(timezone.utc)
    now_tr = now_utc + timedelta(hours=3)
    months_tr = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
                 "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
    months_en = ["January", "February", "March", "April", "May", "June",
                 "July", "August", "September", "October", "November", "December"]
    human_time_tr = f"{now_tr.day} {months_tr[now_tr.month - 1]} {now_tr.year} {now_tr.strftime('%H:%M')}"
    human_time_en = f"{months_en[now_utc.month - 1]} {now_utc.day}, {now_utc.year} {now_utc.strftime('%H:%M')} UTC"

    cve_count = sum(1 for it in final_items if it.get("is_cve"))

    radar_payload = {
        "last_updated": now_tr.strftime("%Y-%m-%dT%H:%M:%S+03:00"),
        "last_updated_human": human_time_tr,
        "last_updated_human_tr": human_time_tr,
        "last_updated_human_en": human_time_en,
        "total_items": len(final_items),
        "cve_alerts_count": cve_count,
        "kernel_watchdog": kernel_status,
        "categories": CATEGORY_MAP,
        "items": final_items
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(radar_payload, f, ensure_ascii=False, indent=2)

    print(f"[+] radar.json updated: {len(final_items)} items ({cve_count} CVE alerts)")
    return radar_payload

def generate_daily_digest(radar_data):
    """Generates bilingual daily digest posts in content/posts/radar/ with cross-references and CVE links."""
    os.makedirs(POSTS_RADAR_DIR, exist_ok=True)

    now_utc = datetime.now(timezone.utc)
    now_tr = now_utc + timedelta(hours=3)
    months_tr = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
                 "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
    months_en = ["January", "February", "March", "April", "May", "June",
                 "July", "August", "September", "October", "November", "December"]
    date_str = now_tr.strftime("%Y-%m-%d")
    human_date_tr = f"{now_tr.day} {months_tr[now_tr.month - 1]} {now_tr.year}"
    human_date_en = f"{months_en[now_utc.month - 1]} {now_utc.day}, {now_utc.year}"

    all_items = radar_data.get("items", [])
    if not all_items:
        print("[!] No items available to generate digest.")
        return None

    kernel_info = radar_data.get("kernel_watchdog", {})

    cve_items = [it for it in all_items if it.get("is_cve") or it.get("is_critical")][:4]
    kernel_items = [it for it in all_items if it.get("category") == "kernel" and it not in cve_items][:3]
    infra_items = [it for it in all_items if it.get("category") == "infra" and it not in cve_items][:3]
    security_items = [it for it in all_items if it.get("category") == "security" and it not in cve_items][:3]
    github_items = [it for it in all_items if it.get("category") == "github"][:3]
    ai_items = [it for it in all_items if it.get("category") == "ai" and it not in cve_items][:3]
    comm_items = [it for it in all_items if it.get("category") == "community" and it not in cve_items][:3]

    # --- 1. TURKISH DIGEST POST ---
    content_tr = []
    content_tr.append("---")
    content_tr.append(f"title: \"Sistem ve Güvenlik Radarı: {human_date_tr}\"")
    content_tr.append(f"date: {now_tr.strftime('%Y-%m-%dT%H:%M:%S+03:00')}")
    content_tr.append("draft: false")
    content_tr.append("tags: [\"Radar\", \"Sistem\", \"Güvenlik\", \"Linux\", \"CVE\"]")
    content_tr.append("categories: [\"Radar\"]")
    content_tr.append("summary: \"Günün öne çıkan Linux çekirdeği güncellemeleri, kritik CVE/güvenlik analizleri, depolama mimarileri ve altyapı gelişmeleri.\"")
    content_tr.append(f"translationKey: \"radar-{date_str}\"")
    content_tr.append("showToc: true")
    content_tr.append("TocOpen: false")
    content_tr.append("---")
    content_tr.append("")
    content_tr.append(f"> **OPERASYONEL RAPOR // {date_str}**  ")
    content_tr.append(f"> Bu bülten, [Canlı Sistem Radarı](/radar/) üzerinde toplanan 25+ otoriter kaynaktan derlenmiş; kural tabanlı puanlama ile filtrelenmiş günün en kritik gelişmelerini içerir.")
    content_tr.append("")

    if kernel_info.get("status") == "ONLINE":
        content_tr.append(f"> 🐧 **Kernel.org Canlı Takipçisi:** `Mainline: {kernel_info.get('mainline')}` | `Stable: {kernel_info.get('stable')}` | `LTS: {kernel_info.get('longterm')}`")
        content_tr.append("")

    if cve_items:
        content_tr.append("## 🚨 Kritik Güvenlik ve CVE Uyarıları")
        content_tr.append("")
        for it in cve_items:
            cve_link = f"**[[{it['cve_id']}](https://nvd.nist.gov/vuln/detail/{it['cve_id']})]** " if it.get("cve_id") else "**[KRİTİK TEHDİT]** "
            content_tr.append(f"### {cve_link}[{it['title']}]({it['link']})")
            meta_parts = [f"**Kaynak:** `{it['source']}`", f"**Skor:** `{it['score']}/10`"]
            if it.get("comments_url"):
                meta_parts.append(f"[💬 Topluluk Tartışması]({it['comments_url']})")
            content_tr.append(f"- {' | '.join(meta_parts)}")
            content_tr.append(f"- **Özet:** {it['summary']}")
            if it.get("related_post"):
                rel = it["related_post"]
                content_tr.append(f"> 🔗 **İlgili Teknik Analizim:** [{rel['title_tr']}]({rel['url_tr']})")
            content_tr.append("")

    if kernel_items:
        content_tr.append("## 🐧 Linux Çekirdeği, Donanım ve Sistem")
        content_tr.append("")
        for it in kernel_items:
            discuss = f" · [Tartışma]({it['comments_url']})" if it.get("comments_url") else ""
            content_tr.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`{discuss})  \n  {it['summary']}")
            if it.get("related_post"):
                rel = it["related_post"]
                content_tr.append(f"  > 🔗 *İlgili Analiz:* [{rel['title_tr']}]({rel['url_tr']})")
        content_tr.append("")

    if security_items:
        content_tr.append("## 🛡️ Altyapı Güvenliği & Tehdit İstihbaratı")
        content_tr.append("")
        for it in security_items:
            discuss = f" · [Tartışma]({it['comments_url']})" if it.get("comments_url") else ""
            content_tr.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`{discuss})  \n  {it['summary']}")
            if it.get("related_post"):
                rel = it["related_post"]
                content_tr.append(f"  > 🔗 *İlgili Analiz:* [{rel['title_tr']}]({rel['url_tr']})")
        content_tr.append("")

    if github_items:
        content_tr.append("## 🐙 Açık Kaynak Sistemler ve eBPF Repoları")
        content_tr.append("")
        for it in github_items:
            content_tr.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`)  \n  {it['summary']}")
        content_tr.append("")

    if infra_items:
        content_tr.append("## ⚙️ Depolama, Ağ ve Kurumsal Mimariler")
        content_tr.append("")
        for it in infra_items:
            content_tr.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`)  \n  {it['summary']}")
            if it.get("related_post"):
                rel = it["related_post"]
                content_tr.append(f"  > 🔗 *İlgili Analiz:* [{rel['title_tr']}]({rel['url_tr']})")
        content_tr.append("")

    if ai_items:
        content_tr.append("## ⚡ Yapay Zeka Altyapısı ve GPU Hesaplama")
        content_tr.append("")
        for it in ai_items:
            discuss = f" · [Tartışma]({it['comments_url']})" if it.get("comments_url") else ""
            content_tr.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`{discuss})  \n  {it['summary']}")
        content_tr.append("")

    if comm_items:
        content_tr.append("## 🌐 Mühendislik Topluluğu ve Tartışmalar")
        content_tr.append("")
        for it in comm_items:
            discuss = f" · [Topluluk Tartışması]({it['comments_url']})" if it.get("comments_url") else ""
            content_tr.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`{discuss})  \n  {it['summary']}")
        content_tr.append("")

    content_tr.append("---")
    content_tr.append("📡 *Tüm canlı akışı ve anlık filtrelemeyi [onurbaydas.online/radar](/radar/) sayfasından takip edebilir, kaynak listesini [OPML formatında](/radar/sources.opml) indirebilirsiniz.*")

    post_path_tr = os.path.join(POSTS_RADAR_DIR, f"{date_str}-sistem-ve-guvenlik-radari.md")
    with open(post_path_tr, "w", encoding="utf-8") as f:
        f.write("\n".join(content_tr))

    # --- 2. ENGLISH DIGEST POST ---
    content_en = []
    content_en.append("---")
    content_en.append(f"title: \"Systems & Security Radar: {human_date_en}\"")
    content_en.append(f"date: {now_tr.strftime('%Y-%m-%dT%H:%M:%S+03:00')}")
    content_en.append("draft: false")
    content_en.append("tags: [\"Radar\", \"Systems\", \"Security\", \"Linux\", \"CVE\"]")
    content_en.append("categories: [\"Radar\"]")
    content_en.append("summary: \"Daily operational intelligence covering Linux kernel advancements, critical CVE advisories, enterprise storage, and infrastructure architecture.\"")
    content_en.append(f"translationKey: \"radar-{date_str}\"")
    content_en.append("showToc: true")
    content_en.append("TocOpen: false")
    content_en.append("---")
    content_en.append("")
    content_en.append(f"> **OPERATIONAL REPORT // {date_str}**  ")
    content_en.append(f"> Curated intelligence bulletin aggregated from 25+ authoritative engineering hubs via the [Live Systems Radar](/en/radar/). Heuristically filtered for systems architecture and security severity.")
    content_en.append("")

    if kernel_info.get("status") == "ONLINE":
        content_en.append(f"> 🐧 **Kernel.org Live Watchdog:** `Mainline: {kernel_info.get('mainline')}` | `Stable: {kernel_info.get('stable')}` | `LTS: {kernel_info.get('longterm')}`")
        content_en.append("")

    if cve_items:
        content_en.append("## 🚨 Critical Security & CVE Advisories")
        content_en.append("")
        for it in cve_items:
            cve_link = f"**[[{it['cve_id']}](https://nvd.nist.gov/vuln/detail/{it['cve_id']})]** " if it.get("cve_id") else "**[CRITICAL THREAT]** "
            content_en.append(f"### {cve_link}[{it['title']}]({it['link']})")
            meta_parts = [f"**Source:** `{it['source']}`", f"**Score:** `{it['score']}/10`"]
            if it.get("comments_url"):
                meta_parts.append(f"[💬 Discussion Wire]({it['comments_url']})")
            content_en.append(f"- {' | '.join(meta_parts)}")
            content_en.append(f"- **Summary:** {it['summary']}")
            if it.get("related_post"):
                rel = it["related_post"]
                content_en.append(f"> 🔗 **Related Architecture Breakdown:** [{rel['title_en']}]({rel['url_en']})")
            content_en.append("")

    if kernel_items:
        content_en.append("## 🐧 Linux Kernel, Hardware & Low-Level Systems")
        content_en.append("")
        for it in kernel_items:
            discuss = f" · [Discussion]({it['comments_url']})" if it.get("comments_url") else ""
            content_en.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`{discuss})  \n  {it['summary']}")
            if it.get("related_post"):
                rel = it["related_post"]
                content_en.append(f"  > 🔗 *Related Breakdown:* [{rel['title_en']}]({rel['url_en']})")
        content_en.append("")

    if security_items:
        content_en.append("## 🛡️ Infrastructure Security & Threat Intelligence")
        content_en.append("")
        for it in security_items:
            discuss = f" · [Discussion]({it['comments_url']})" if it.get("comments_url") else ""
            content_en.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`{discuss})  \n  {it['summary']}")
            if it.get("related_post"):
                rel = it["related_post"]
                content_en.append(f"  > 🔗 *Related Breakdown:* [{rel['title_en']}]({rel['url_en']})")
        content_en.append("")

    if github_items:
        content_en.append("## 🐙 Open Source Systems & eBPF Tools")
        content_en.append("")
        for it in github_items:
            content_en.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`)  \n  {it['summary']}")
        content_en.append("")

    if infra_items:
        content_en.append("## ⚙️ Enterprise Storage, Networking & Cloud Architecture")
        content_en.append("")
        for it in infra_items:
            content_en.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`)  \n  {it['summary']}")
            if it.get("related_post"):
                rel = it["related_post"]
                content_en.append(f"  > 🔗 *Related Breakdown:* [{rel['title_en']}]({rel['url_en']})")
        content_en.append("")

    if ai_items:
        content_en.append("## ⚡ AI Infrastructure & GPU Acceleration")
        content_en.append("")
        for it in ai_items:
            discuss = f" · [Discussion]({it['comments_url']})" if it.get("comments_url") else ""
            content_en.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`{discuss})  \n  {it['summary']}")
        content_en.append("")

    if comm_items:
        content_en.append("## 🌐 Engineering Community & Technical Debates")
        content_en.append("")
        for it in comm_items:
            discuss = f" · [Community Discussion]({it['comments_url']})" if it.get("comments_url") else ""
            content_en.append(f"- **[{it['title']}]({it['link']})** (`{it['source']}`{discuss})  \n  {it['summary']}")
        content_en.append("")

    content_en.append("---")
    content_en.append("📡 *Inspect the full interactive live stream at [onurbaydas.online/en/radar](/en/radar/) or download the curated feed list in [OPML format](/radar/sources.opml).*")

    post_path_en = os.path.join(POSTS_RADAR_DIR, f"{date_str}-sistem-ve-guvenlik-radari.en.md")
    with open(post_path_en, "w", encoding="utf-8") as f:
        f.write("\n".join(content_en))

    print(f"[+] Bilingual daily digest posts generated:\n    TR: {post_path_tr}\n    EN: {post_path_en}")
    return post_path_tr

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
    parser = argparse.ArgumentParser(description="OB Systems Radar Engine (Advanced)")
    parser.add_argument("--digest", action="store_true", help="Generate daily digest blog post")
    parser.add_argument("--telegram", action="store_true", help="Send alert to Telegram")
    args = parser.parse_args()

    print(f"[*] Radar run started at: {datetime.now(timezone.utc).isoformat()}")

    # 1. Fetch live kernel.org status
    kernel_status = fetch_kernel_watchdog()

    # 2. Fetch OPML sources
    sources = load_sources_from_opml(OPML_FILE)
    print(f"[*] Loaded {len(sources)} sources from {OPML_FILE}")

    all_items = []
    for s in sources:
        items = fetch_feed(s)
        if items:
            all_items.extend(items)
        time.sleep(0.2)

    # 3. Fetch GitHub Systems Trending
    git_items = fetch_github_systems_trending()
    if git_items:
        all_items.extend(git_items)

    print(f"[*] Total fetched raw items: {len(all_items)}")
    radar_data = update_radar_data(all_items, kernel_status)

    if args.digest:
        generate_daily_digest(radar_data)

    if args.telegram or os.getenv("TELEGRAM_BOT_TOKEN"):
        send_telegram_alert(radar_data)

    print("[*] Radar execution completed successfully.")

if __name__ == "__main__":
    main()
