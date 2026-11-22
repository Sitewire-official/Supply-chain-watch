import json, re, os, time, html, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

CFG = json.load(open("config.json"))
NOW = datetime.now(timezone.utc)
UA = {"User-Agent": "SupplyChainWatch/1.0 (news aggregator; links to originals)"}

def load(p, d):
    try: return json.load(open(p))
    except Exception: return d

store = load("stories.json", {"updated": None, "stories": []})
stories = store["stories"]
pending = load("pending.json", [])
seen = set(load("seen.json", []))

def get(url, t=25):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=t).read()

def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()

def queries():
    q = []
    for co in CFG["companies"]:
        terms = [m["name"] for m in CFG["materials"]] + CFG["extra_terms"]
        for t in terms:
            q.append(f'"{co["name"]}" {t}')
        d = CFG["source_domains"]
        for i in range(0, len(d), 5):
            q.append(f'"{co["name"]}" (' + " OR ".join("site:" + x for x in d[i:i+5]) + ")")
    return ["https://www.bing.com/news/search?format=rss&mkt=en-GB&q=" + urllib.parse.quote(x) for x in q] + CFG["rss_feeds"]

def resolve(link):
    if "bing.com" in link:
        u = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url")
        if u: return u[0]
    return link

def read_feed(u):
    out = []
    try: root = ET.fromstring(get(u))
    except Exception as e:
        print("FEED FAILED:", u[:90], e); return out
    for it in root.iter("item"):
        g = lambda k: (it.findtext(k) or "").strip()
        try: d = parsedate_to_datetime(g("pubDate")).astimezone(timezone.utc)
        except Exception: d = NOW
        src = next((c.text for c in it if c.tag.endswith("Source") and c.text), None)
        url = resolve(g("link"))
        out.append({"title": clean(g("title")), "url": url, "snippet": clean(g("description")), "date": d,
                    "source": src or urllib.parse.urlparse(url).netloc})
    return out

def article(url):
    try: raw = get(url).decode("utf-8", "ignore")
    except Exception: return "", None
    img = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', raw)
    text = " ".join(clean(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", raw, re.S))
    return text[:6000], (img.group(1) if img else None)

def pat(names):
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(n) for n in names) + r")(?!\w)", re.I)

def classify(title, body):
    """Return (verdict, matches). verdict: publish / review / None."""
    full = title + " " + body
    if any(x.lower() in full.lower() for x in CFG["exclude_phrases"]): return None, []
    low = full.lower()
    kws = [k for m in CFG["materials"] for k in m["keywords"] if k in low] + [k for k in CFG["topic_keywords"] if k in low]
    if not kws: return None, []
    lead = title + " " + body[:1000]
    res = {}
    for co in CFG["companies"]:
        if pat(co["strong"]).search(lead): res[co["name"]] = "publish"
        elif pat(co["weak"]).search(lead) and pat([co["name"]]).search(full): res[co["name"]] = "publish"
        elif pat(co["strong"]).search(full): res[co["name"]] = "review"
    if not res: return None, []
    return ("publish" if "publish" in res.values() else "review"), [c for c, v in res.items() if v == "publish"] or list(res)

SYS = ("You write detailed, neutral news summaries for a supply-chain monitoring site covering Pladis and Reckitt. "
       "Use ONLY facts in the provided text. Never speculate or add outside knowledge. If the text is short (paywalled), write only what it supports. "
       'Reply with JSON only: {"about_company": true/false (is the article substantively about the named company, not a passing mention or a different entity with a similar name), '
       '"headline": str, "summary": [3-5 paragraphs of plain text], '
       '"categories": subset of ["Business & Commercial","Economic","Sustainability","ESG"], '
       '"materials": subset of ["Palm oil","Pulp & paper","Cocoa","Latex"]}')

def ask(title, body, cos):
    payload = json.dumps({"model": CFG["model"], "temperature": 0.2, "messages": [
        {"role": "system", "content": SYS},
        {"role": "user", "content": f"Company: {', '.join(cos)}\nTitle: {title}\nText: {body[:5000]}"}]}).encode()
    req = urllib.request.Request("https://models.github.ai/inference/chat/completions", payload,
        {"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"], "Content-Type": "application/json",
         "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        raw = urllib.request.urlopen(req, timeout=120).read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:300]))
    try:
        txt = json.loads(raw)["choices"][0]["message"]["content"] or ""
    except Exception:
        raise RuntimeError("Unexpected API reply: " + raw[:300])
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise RuntimeError("Model reply had no JSON: " + txt[:300])
    return json.loads(m.group(0))

def toks(s): return set(re.findall(r"[a-z]{4,}", s.lower()))

def find_story(title, cos):
    t = toks(title)
    for s in stories:
        if set(cos) & set(s["companies"]) and len(t & toks(s["title"])) / max(1, len(t | toks(s["title"]))) >= 0.4: return s
    return None

cands = {}
for u in queries():
    for c in read_feed(u): cands.setdefault(c["url"], c)
    time.sleep(1)
print("candidates:", len(cands))

done = 0
for url, c in cands.items():
    if url in seen or (NOW - c["date"]).days > CFG["max_age_days"]: continue
    if done >= CFG["daily_cap"]: print("daily cap reached"); break
    body, img = article(url)
    body = body or c["snippet"]
    verdict, cos = classify(c["title"], body)
    if not verdict: seen.add(url); continue
    if verdict == "review":
        pending.append({"title": c["title"], "url": url, "source": c["source"], "companies": cos, "reason": "Company not in headline/opening"})
        seen.add(url); continue
    try: ai = ask(c["title"], body, cos)
    except Exception as e:
        print("AI FAILED (will retry next run):", e); continue
    done += 1; seen.add(url)
    if not ai.get("about_company"):
        pending.append({"title": c["title"], "url": url, "source": c["source"], "companies": cos, "reason": "AI check: not substantively about company"}); continue
    src = {"name": c["source"], "url": url, "date": c["date"].isoformat()}
    cats = [x for x in ai.get("categories", []) if x in ["Business & Commercial", "Economic", "Sustainability", "ESG"]]
    mats = [x for x in ai.get("materials", []) if x in [m["label"] for m in CFG["materials"]]]
    s = find_story(c["title"], cos)
    paras = [p for p in ai.get("summary", []) if isinstance(p, str)]
    if s:
        s["sources"].append(src); s["updated"] = NOW.isoformat()
        s["categories"] = sorted(set(s["categories"]) | set(cats)); s["materials"] = sorted(set(s["materials"]) | set(mats))
        if paras and len(s["summary"]) < 9: s["summary"].append("Update, " + NOW.strftime("%d %b") + ": " + paras[0])
    else:
        stories.append({"id": str(abs(hash(url)))[:10], "title": ai.get("headline") or c["title"], "companies": cos, "categories": cats, "materials": mats,
            "summary": paras, "sources": [src], "image": img, "first_seen": NOW.isoformat(), "updated": NOW.isoformat(),
            "matched": f"{', '.join(cos)} named in headline or opening paragraphs; topic keywords found; AI relevance check passed"})

days = CFG["expiry_days"]
if sum((NOW - datetime.fromisoformat(s["updated"])).days < days for s in stories) < CFG["min_stories"]:
    days = CFG["extended_expiry_days"]
stories[:] = [s for s in stories if (NOW - datetime.fromisoformat(s["updated"])).days < days]
store["updated"] = NOW.isoformat()
json.dump(store, open("stories.json", "w"), indent=1, ensure_ascii=False)
json.dump(pending[-200:], open("pending.json", "w"), indent=1, ensure_ascii=False)
json.dump(sorted(seen)[-5000:], open("seen.json", "w"))
print("done; published stories:", len(stories))
