#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
oav — Overleaf Author View

从 Overleaf 多人协作历史中，按作者提取改动，生成「某位老师偏好」的一页 HTML。

  report   : 该作者的改动记录一页 HTML（时间线 + 逐文件增删 diff）
  onepager : 以该作者原话与偏好口径为骨架的一页概览 HTML（启发式，可选 LLM 叙事）

输入（二选一）：
  - Overleaf git 桥克隆目录（git log 按作者带完整提交）
  - JSONL 历史导出，每行一条编辑记录

零第三方依赖，Python 3.9+。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html as _html
import json
import os
import re
import subprocess
import sys
import urllib.request

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
# record = {
#   "author": str, "time": str(ISO), "file": str,
#   "msg": str, "added": [str], "removed": [str],
# }

_TS = r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})"


def parse_time(t):
    if not t:
        return None
    try:
        return _dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except Exception:
        m = re.match(_TS, str(t))
        if m:
            try:
                return _dt.datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M")
            except Exception:
                return None
    return None


def esc(s):
    return _html.escape(str(s))


# ---------------------------------------------------------------------------
# 加载历史
# ---------------------------------------------------------------------------
def load_records(input_path):
    """input_path 为目录 → git 模式；为 .jsonl/.json 文件 → 直接解析。"""
    if os.path.isdir(input_path):
        return records_from_git(input_path)
    with open(input_path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return []
    if raw.startswith("["):
        data = json.loads(raw)
        return [r for r in data if isinstance(r, dict)]
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _git(args, cwd):
    return subprocess.run(
        ["git", "-C", cwd] + args,
        capture_output=True, text=True, timeout=120,
    ).stdout


def records_from_git(repo):
    """Overleaf git 桥克隆 → 归一化记录。每提交一条（或每文件一条）。"""
    log = _git(["log", "--all", "--date=iso-strict",
                "--pretty=format:%H%x1f%an%x1f%aI%x1f%s"], repo)
    records = []
    for line in log.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 4:
            continue
        h, author, when, msg = parts[0], parts[1], parts[2], parts[3]
        diff = _git(["show", "--format=", "--unified=0", h, "--"], repo)
        cur = None
        added, removed = [], []
        for dl in diff.splitlines():
            if dl.startswith("+++ b/") or dl.startswith("--- a/"):
                if cur and (added or removed):
                    records.append(dict(author=author, time=when, file=cur,
                                        msg=msg, added=added, removed=removed))
                cur = dl[6:].strip()
                added, removed = [], []
                continue
            if cur is None:
                continue
            if dl.startswith("+") and not dl.startswith("+++"):
                added.append(dl[1:])
            elif dl.startswith("-") and not dl.startswith("---"):
                removed.append(dl[1:])
        if cur and (added or removed):
            records.append(dict(author=author, time=when, file=cur,
                                msg=msg, added=added, removed=removed))
    return records


# ---------------------------------------------------------------------------
# 过滤与聚合
# ---------------------------------------------------------------------------
def filter_records(records, author):
    if not author:
        return records
    a = author.lower()
    return [r for r in records if a in (r.get("author") or "").lower()]


def aggregate(records):
    files = {}
    authors = {}
    times = []
    total_added = total_removed = 0
    for r in records:
        f = r.get("file") or "(unknown)"
        files[f] = files.get(f, 0) + 1
        au = r.get("author") or "(unknown)"
        authors[au] = authors.get(au, 0) + 1
        t = parse_time(r.get("time"))
        if t:
            times.append(t)
        total_added += len(r.get("added") or [])
        total_removed += len(r.get("removed") or [])
    day_counts = {}
    for t in times:
        day_counts[t.strftime("%Y-%m-%d")] = day_counts.get(t.strftime("%Y-%m-%d"), 0) + 1
    return {
        "n": len(records),
        "files": files,
        "authors": authors,
        "n_days": len(day_counts),
        "day_counts": dict(sorted(day_counts.items())),
        "total_added": total_added,
        "total_removed": total_removed,
        "first": min(times).strftime("%Y-%m-%d") if times else None,
        "last": max(times).strftime("%Y-%m-%d") if times else None,
    }


# ---------------------------------------------------------------------------
# LaTeX 简易解析（够用于一页概览即可，不做完整语法）
# ---------------------------------------------------------------------------
def strip_comments(text):
    return re.sub(r"(?<!\\)%.*", "", text)


def _clean_latex(s):
    s = re.sub(r"\\[a-zA-Z]+\*?", " ", s)          # \cmd
    s = re.sub(r"\\[{}]", "", s)                    # \{ \}
    s = re.sub(r"[{}]", "", s)
    s = re.sub(r"\$+", "", s)
    s = s.replace("\\&", "&").replace("\\%", "%")
    s = s.replace("~", " ").replace("--", "\u2013")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_braced(pattern, text, nl_sep=" · "):
    m = re.search(pattern, text, re.S)
    if not m:
        return ""
    raw = re.sub(r"\\\\", nl_sep, m.group(1))   # \\ 行内换行 → 分隔符
    return _clean_latex(raw)


def parse_doc(text):
    t = strip_comments(text)
    # 只解析正文：preamble 里的模板定义（如 \section*{#1}）不是真实章节
    if "\\begin{document}" in t:
        t = t.split("\\begin{document}", 1)[-1]
    title = extract_braced(r"\\(?:TITLE|title)\{(.+?)\}", t)
    abstract = extract_braced(r"\\(?:ABSTRACT|abstract)\{(.+?)\}", t)
    if not abstract:
        m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", t, re.S)
        if m:
            abstract = _clean_latex(m.group(1))
    keywords = extract_braced(r"\\KEYWORDS\{(.+?)\}", t)
    sections = []
    for m in re.finditer(r"\\(?:sub)?section\*?\{(.+?)\}", t, re.S):
        h = _clean_latex(m.group(1))
        if h and len(h) >= 2 and "#" not in h and h not in sections:
            sections.append(h)
    body = _clean_latex(t)
    return {"title": title, "abstract": abstract, "keywords": keywords,
            "sections": sections, "body": body}


def first_sentences(text, n=3):
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?。！？])\s+", text)
    return " ".join(x for x in parts if x)[:1000]


# ---------------------------------------------------------------------------
# 模板样式
# ---------------------------------------------------------------------------
BASE_CSS = """
:root{--ink:#16242F;--ink-soft:#3D4F5C;--muted:#5F7180;--navy:#1E3A5F;
--navy-2:#2C5178;--blue:#1E6FA8;--blue-soft:#EDF5FB;--line:#D9E1E7;--paper:#fff;--soft:#F5F8FA}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:400 15px/1.78 "Noto Sans SC","PingFang SC","Hiragino Sans GB",sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:1020px;margin:0 auto;padding:54px 30px 40px}
.masthead{padding:0 0 30px;border-bottom:1px solid var(--line)}
.eyebrow{font-size:11.5px;letter-spacing:.2em;color:var(--navy);font-weight:600;margin:0 0 16px}
h1{font-family:"Noto Serif SC",Georgia,"Songti SC",serif;font-size:clamp(26px,4.4vw,40px);
line-height:1.24;font-weight:700;margin:0 0 10px}
.subtitle{font-family:"Noto Serif SC",Georgia,"Songti SC",serif;font-size:clamp(15px,2.1vw,19px);
color:var(--ink-soft);font-weight:600;margin:0 0 16px}
.meta{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:center;margin-top:14px;font-size:12.5px;color:var(--muted)}
.meta b{color:var(--ink-soft);font-weight:600}
section{padding:30px 0 4px;border-top:1px solid var(--line);margin-top:30px}
.sec-head{display:flex;align-items:baseline;gap:12px;margin-bottom:10px}
.sec-no{font-family:"Noto Serif SC",Georgia,serif;font-weight:700;font-size:14px;color:var(--blue);flex:none}
h2{font-family:"Noto Serif SC",Georgia,"Songti SC",serif;font-size:20px;font-weight:700;color:var(--ink);margin:0}
.sec-gloss{font-size:12px;color:var(--muted);margin-left:auto;flex:none}
p{margin:0 0 13px}
.advisor{margin:16px 0;padding:14px 18px;background:var(--blue-soft);border-left:4px solid var(--blue);color:var(--navy)}
.advisor .tag{display:block;font-size:11px;font-weight:600;letter-spacing:.12em;color:var(--blue);margin-bottom:6px}
.advisor p{margin:0 0 8px;color:var(--navy)}.advisor p:last-child{margin:0}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}
.stat{flex:1;min-width:120px;border:1px solid var(--line);border-top:3px solid var(--navy);padding:10px 14px;background:var(--paper)}
.stat b{display:block;font-family:"Noto Serif SC",Georgia,serif;font-size:22px;color:var(--navy)}
.stat span{font-size:12px;color:var(--muted)}
figure{margin:18px 0 4px}
.timeline{list-style:none;margin:12px 0;padding:0}
.timeline li{display:grid;grid-template-columns:88px 1fr;gap:14px;padding:10px 0;border-bottom:1px dashed var(--line)}
.timeline .day{font-weight:600;color:var(--navy);font-size:13px}
.timeline .bar{height:7px;border-radius:4px;background:linear-gradient(90deg,var(--blue) var(--p),#D8E4EE var(--p));margin-top:6px}
.file{font:13px/1.8 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);
border:1px solid var(--line);border-radius:6px;padding:10px 12px;margin:10px 0;overflow-wrap:anywhere;white-space:pre-wrap}
.diff{font:12.5px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;background:#f8fafb;border:1px solid var(--line);
border-radius:6px;padding:9px 11px;margin:6px 0;overflow-wrap:anywhere;white-space:pre-wrap}
ins{background:#dcf6e8;color:#075332;text-decoration:none}
del{background:#ffe5e5;color:#8d2525}
.entry{padding:14px 0;border-bottom:1px solid var(--line)}
.entry .when{font-size:12px;color:var(--muted)}
.entry .f{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;color:var(--navy-2);overflow-wrap:anywhere}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0 2px}
.chip{font-size:11.5px;color:var(--navy-2);background:var(--soft);border:1px solid var(--line);padding:3px 10px;border-radius:14px}
.takeaways{list-style:none;margin:12px 0 4px;padding:0}
.takeaways li{position:relative;padding:0 0 0 24px;margin:0 0 10px;color:var(--ink-soft)}
.takeaways li::before{content:"";position:absolute;left:2px;top:10px;width:10px;height:10px;border:2px solid var(--blue);border-radius:50%}
footer{border-top:1px solid var(--line);margin-top:40px;padding-top:20px;font-size:12.5px;color:var(--muted);line-height:1.7}
.toolbar{margin:16px 0 0;display:flex;justify-content:flex-end}
.print-btn{font:500 13px "Noto Sans SC",sans-serif;color:#fff;background:var(--navy);border:0;border-radius:6px;padding:9px 18px;cursor:pointer}
.print-btn:hover{background:var(--navy-2)}
@media(max-width:720px){main{padding:36px 16px 28px}.sec-gloss{display:none}.timeline li{grid-template-columns:1fr;gap:2px}}
@media print{body{font-size:12px;line-height:1.6}main{max-width:none;padding:0}.toolbar{display:none}
section,figure,.entry,.advisor{break-inside:avoid}section{border-top:1px solid #999}}
"""


def _page(title, head_html, body_html, doc_title):
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>{BASE_CSS}</style></head>
<body><main>
{head_html}
{body_html}
<div class="toolbar"><button class="print-btn" onclick="window.print()">打印 / 导出 PDF</button></div>
</main></body></html>
"""


# ---------------------------------------------------------------------------
# report：改动记录
# ---------------------------------------------------------------------------
def render_report(records, meta, author, project_label="Overleaf project"):
    head = f"""<header class="masthead">
<p class="eyebrow">{esc(project_label)} · 修改记录 · OVERLEAF AUTHOR VIEW</p>
<h1>{esc(author or "全部作者")} 的改动记录</h1>
<div class="meta">
<span><b>{meta['n']}</b> 条编辑</span>
<span><b>{len(meta['files'])}</b> 个文件</span>
<span><b>{meta['n_days']}</b> 天</span>
<span><b>{meta['total_added']}</b> 行新增 · <b>{meta['total_removed']}</b> 行删除</span>
<span>{esc(meta['first'] or '—')} → {esc(meta['last'] or '—')}</span>
</div></header>"""

    timeline = ""
    if meta["day_counts"]:
        mx = max(meta["day_counts"].values()) or 1
        lis = []
        for d, c in meta["day_counts"].items():
            p = round(100 * c / mx)
            lis.append(f'<li><div class="day">{d}</div>'
                       f'<div>{c} 条编辑<div class="bar" style="--p:{p}%"></div></div></li>')
        timeline = (f'<section><div class="sec-head"><span class="sec-no">01</span>'
                    f'<h2>时间线</h2><span class="sec-gloss">Timeline</span></div>'
                    f'<ul class="timeline">{"".join(lis)}</ul></section>')

    body = []
    per_file = {}
    for r in sorted(records, key=lambda x: x.get("time") or ""):
        per_file.setdefault(r.get("file") or "(unknown)", []).append(r)
    for f, rs in sorted(per_file.items()):
        body.append(f'<section><div class="sec-head"><span class="sec-no">02</span>'
                    f'<h2>{esc(f)}</h2><span class="sec-gloss">{len(rs)} 条</span></div>')
        for r in rs:
            when = esc((r.get("time") or "").replace("T", " ")[:16])
            msg = esc(r.get("msg") or "")
            added = "".join(f"<ins>{esc(x)}</ins>\n" for x in (r.get("added") or []))
            removed = "".join(f"<del>{esc(x)}</del>\n" for x in (r.get("removed") or []))
            diffs = ""
            if added:
                diffs += f'<div class="diff">{added}</div>'
            if removed:
                diffs += f'<div class="diff">{removed}</div>'
            if not diffs:
                diffs = '<div class="diff"><span style="color:var(--muted)">（无文本增删）</span></div>'
            body.append(f'<div class="entry"><div class="when">{when}'
                        + (f' · {msg}' if msg else "")
                        + f'</div>{diffs}</div>')
        body.append("</section>")

    footer = (f'<footer>由 oav 生成 · 来源：{esc(project_label)} · '
              f'作者：{esc(", ".join(meta["authors"]) or "—")}'
              f'</footer>')
    return _page(f"{author or 'All'} — changes", head, timeline + "".join(body) + footer,
                 doc_title=f"{author or 'All'} 的改动记录")


# ---------------------------------------------------------------------------
# onepager：老师偏好一页概览
# ---------------------------------------------------------------------------
def render_onepager(doc, records, author, profile=None, llm_out=None):
    profile = profile or {}
    pname = profile.get("name") or author or "该老师"
    framing = profile.get("framing") or ""
    chips = profile.get("chips") or []
    title = doc.get("title") or "Untitled"
    subtitle = profile.get("subtitle") or "One-Page Summary"
    keywords = doc.get("keywords") or ""
    abstract = doc.get("abstract") or ""
    sections = doc.get("sections") or []

    # 老师原话 = 其历史中新增的句子（清洗后，去重）
    seen, words = set(), []
    for r in sorted(records, key=lambda x: x.get("time") or ""):
        for line in r.get("added") or []:
            s = _clean_latex(line).strip()
            if len(s) < 24 or s in seen:
                continue
            seen.add(s)
            words.append(s)
            if len(words) >= 10:
                break
        if len(words) >= 10:
            break

    lede = ""
    if abstract:
        lede = first_sentences(abstract, 2)
    elif doc.get("body"):
        lede = first_sentences(doc["body"], 2)

    head = f"""<header class="masthead">
<p class="eyebrow">WORKING PAPER · ONE-PAGE SUMMARY · {esc(author or 'AUTHOR')}</p>
<h1>{esc(title)}</h1>
<p class="subtitle">{esc(subtitle)}</p>
<div class="meta"><span><b>{esc(keywords)}</b></span>
<span>蓝字为该老师历史改动中的原话</span></div></header>"""

    body = []
    body.append('<section><div class="sec-head"><span class="sec-no">01</span>'
                '<h2>The Question</h2><span class="sec-gloss">核心问题</span></div>'
                f'<p class="lede">{esc(lede) or "（无摘要，请提供 --doc 的正文）"}</p></section>')

    if words:
        quotes = "".join(f"<p>{esc(w)}</p>" for w in words)
        body.append(f'<section><div class="sec-head"><span class="sec-no">02</span>'
                    f'<h2>{esc(pname)} 的原话</h2><span class="sec-gloss">该老师的改动</span></div>'
                    f'<div class="advisor"><span class="tag">原话 · 来自历史改动</span>{quotes}</div></section>')

    sec_items = "".join(
        f"<li>{esc(h)}</li>" for h in sections[:16] if h)
    body.append(f'<section><div class="sec-head"><span class="sec-no">03</span>'
                f'<h2>Document Structure</h2><span class="sec-gloss">章节结构</span></div>'
                f'<ul class="takeaways">{sec_items or "<li>（未识别到章节）</li>"}</ul></section>')

    stats = aggregate(records)
    chips_html = "".join(f'<span class="chip">{esc(c)}</span>' for c in chips)
    body.append('<section><div class="sec-head"><span class="sec-no">04</span>'
                '<h2>Change Profile</h2><span class="sec-gloss">改动概况</span></div>'
                '<div class="stats">'
                f'<div class="stat"><b>{stats["n"]}</b><span>条编辑</span></div>'
                f'<div class="stat"><b>{len(stats["files"])}</b><span>个文件</span></div>'
                f'<div class="stat"><b>{stats["total_added"]}</b><span>行新增</span></div>'
                f'<div class="stat"><b>{stats["total_removed"]}</b><span>行删除</span></div>'
                '</div>'
                + (f'<p style="color:var(--ink-soft)">{esc(framing)}</p>' if framing else "")
                + (f'<div class="chips">{chips_html}</div>' if chips_html else "")
                + '</section>')

    if llm_out:
        extra = f'<section><div class="sec-head"><span class="sec-no">05</span>' \
                f'<h2>Narrative</h2><span class="sec-gloss">LLM 叙事</span></div>' \
                f'<p>{esc(llm_out)}</p></section>'
        body.append(extra)

    footer = (f'<footer><b>来源与口径。</b>本页由 oav 生成：文档 '
              f'+ {esc(author or "该老师")} 的历史改动 + 偏好画像。'
              f'蓝字块为该老师在 Overleaf 历史中的原话；数字随历史记录。'
              f'</footer>')
    return _page(f"{title} — {pname} 偏好一页",
                 head, "".join(body) + footer, doc_title=title)


# ---------------------------------------------------------------------------
# LLM 叙事模式（可选，OpenAI 兼容接口）
# ---------------------------------------------------------------------------
def llm_narrative(doc, records, author, profile, endpoint, model, api_key):
    words = []
    for r in records:
        for line in (r.get("added") or [])[:6]:
            s = _clean_latex(line).strip()
            if len(s) >= 24:
                words.append(s)
        if len(words) >= 8:
            break
    prompt = (
        "你是学术论文一页概览的撰写者。请根据给定文档信息与一位教师的改动，"
        "用这位教师偏好的表述口径写一段 2-4 句的叙事段落（中文），"
        "突出其改动带来的核心判断。只输出 JSON：{\"narrative\":\"...\"}。\n\n"
        f"文档标题：{doc.get('title')}\n摘要：{doc.get('abstract','')[:800]}\n"
        f"章节：{', '.join((doc.get('sections') or [])[:12])}\n"
        f"教师偏好画像：{json.dumps(profile, ensure_ascii=False)}\n"
        f"该教师历史改动的原话：\n" + "\n".join(f"- {w}" for w in words)
    )
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "你是严谨的学术写作助手，输出合法 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 400,
    }).encode("utf-8")
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return content.strip()
    return json.loads(m.group(0)).get("narrative", content.strip())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_report(args):
    records = load_records(args.input)
    records = filter_records(records, args.author)
    if not records:
        print(f"未找到作者「{args.author}」的任何改动记录。")
        sys.exit(1)
    meta = aggregate(records)
    html_out = render_report(records, meta, args.author or "全部作者",
                             project_label=args.project or args.input)
    _write(args.output, html_out)
    print(f"已生成：{args.output}（{meta['n']} 条记录）")


def cmd_onepager(args):
    with open(args.doc, "r", encoding="utf-8") as f:
        doc = parse_doc(f.read())
    records = filter_records(load_records(args.changes), args.author)
    profile = {}
    if args.profile:
        with open(args.profile, "r", encoding="utf-8") as f:
            profile = json.load(f)
    llm_out = None
    if args.llm:
        api_key = args.llm_key or os.environ.get("OAV_LLM_KEY")
        if not api_key:
            print("提示：--llm 需要 --llm-key 或环境变量 OAV_LLM_KEY，已回退到启发式模式。")
        else:
            try:
                llm_out = llm_narrative(doc, records, args.author, profile,
                                        args.llm_endpoint, args.llm_model, api_key)
            except Exception as e:
                print(f"LLM 调用失败（{e}），已回退到启发式模式。")
    html_out = render_onepager(doc, records, args.author, profile, llm_out)
    _write(args.output, html_out)
    print(f"已生成：{args.output}")


def _write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def main():
    ap = argparse.ArgumentParser(
        prog="oav", description="Overleaf Author View — 按作者提取改动，生成老师偏好的一页 HTML")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("report", help="生成某位作者的改动记录一页 HTML")
    p1.add_argument("--input", required=True, help="git 目录 或 history.jsonl")
    p1.add_argument("--author", default="", help="作者名（子串匹配，留空=全部）")
    p1.add_argument("--output", default="report.html")
    p1.add_argument("--project", default="", help="项目标签")
    p1.set_defaults(fn=cmd_report)

    p2 = sub.add_parser("onepager", help="生成老师偏好的一页概览 HTML")
    p2.add_argument("--doc", required=True, help="当前主文档（.tex / 文本）")
    p2.add_argument("--changes", required=True, help="历史：git 目录 或 JSONL")
    p2.add_argument("--author", default="", help="老师作者名")
    p2.add_argument("--profile", default="", help="老师偏好画像 profile.json")
    p2.add_argument("--output", default="onepager.html")
    p2.add_argument("--llm", action="store_true", help="启用 LLM 叙事模式")
    p2.add_argument("--llm-endpoint", default="https://api.deepseek.com/v1")
    p2.add_argument("--llm-model", default="deepseek-chat")
    p2.add_argument("--llm-key", default="")
    p2.set_defaults(fn=cmd_onepager)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
