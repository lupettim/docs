#!/usr/bin/env python3
"""Convert Doxygen C++ HTML (cpp-autodoc-html/) into Mintlify MDX.

Hybrid engine:
  - BeautifulSoup drives the structured regions (member signatures, code
    fragments, parameter/enum tables) so C++ code and prototypes survive as
    real fenced ```cpp blocks instead of markitdown's mangled tables.
  - A markdown-aware DOM walker converts prose while entity-escaping the
    characters that break MDX ( < > { } * ).

It also regenerates the "API Reference" navigation group in docs.json.

Usage:
  python scripts/convert_cpp_autodoc.py            # full run
  python scripts/convert_cpp_autodoc.py --limit 5  # first N pages (per kind mix)
  python scripts/convert_cpp_autodoc.py --only class_im_fusion_1_1_algorithm.html
  python scripts/convert_cpp_autodoc.py --no-nav   # skip docs.json update
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "cpp-autodoc-html"
OUT_BASE = ROOT / "cpp-sdk" / "4.5" / "api"
IMG_OUT = ROOT / "images" / "cpp-api"
ROUTE_BASE = "/cpp-sdk/4.5/api"
IMG_ROUTE = "/images/cpp-api"
NAV_ROUTE_BASE = "cpp-sdk/4.5/api"  # docs.json uses extensionless, no leading slash

KIND_DIR = {
    "class": "classes",
    "struct": "structs",
    "union": "unions",
    "namespace": "namespaces",
    "example": "examples",
    "guide": "guides",
}

TITLE_SUFFIXES = [
    " Class Template Reference",
    " Struct Template Reference",
    " Class Reference",
    " Struct Reference",
    " Union Reference",
    " Namespace Reference",
    " Reference",
]

REIMPL_PREFIXES = (
    "Implemented in ",
    "Implements ",
    "Reimplemented in ",
    "Reimplemented from ",
)


# --------------------------------------------------------------------------- #
# Classification & discovery
# --------------------------------------------------------------------------- #
def classify(name: str) -> str | None:
    if name.endswith("-members.html"):
        return None
    if name.endswith("-example.html"):
        return "example"
    if name.startswith("class"):
        return "class"
    if name.startswith("struct"):
        return "struct"
    if name.startswith("union"):
        return "union"
    if name.startswith("namespace"):
        # skip namespacemembers_*.html index pages
        if name.startswith("namespacemembers"):
            return None
        return "namespace"
    # guide / related pages: leading underscore, not a file (_8) or changelog
    if name.startswith("_") and "_8" not in name and name != "_changelog.html":
        return "guide"
    return None


def slugify(text: str) -> str:
    text = re.sub(r"<[^>]*>", "", text)  # drop template args
    text = text.replace("::", "-")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-") or "page"


def mintlify_anchor(heading: str) -> str:
    h = re.sub(r"<[^>]*>", "", heading)
    h = h.lower()
    h = re.sub(r"[^a-z0-9]+", "-", h)
    return re.sub(r"-+", "-", h).strip("-")


def extract_title(html: str) -> str:
    # capture only the plain text before any nested tag (drops the .ingroups
    # breadcrumb Doxygen embeds inside div.title)
    m = re.search(r'<div class="title">([^<]*)', html)
    if not m:
        return ""
    return BeautifulSoup(m.group(1), "lxml").get_text(" ", strip=True)


def clean_qualname(title: str) -> str:
    t = title.strip()
    # drop trailing state words
    t = re.sub(r"\s+(abstract|final)$", "", t)
    for suf in TITLE_SUFFIXES:
        if t.endswith(suf):
            t = t[: -len(suf)]
            break
    return t.strip()


# --------------------------------------------------------------------------- #
# MDX-safe text escaping
# --------------------------------------------------------------------------- #
def esc(text: str) -> str:
    text = text.replace("{", "&#123;").replace("}", "&#125;")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("*", "\\*")
    return text


def clean_signature(proto: Tag) -> tuple[str, list[str]]:
    labels = [s.get_text(strip=True) for s in proto.find_all("span", class_="mlabel")]
    clone = BeautifulSoup(str(proto), "lxml")
    for s in clone.find_all("span", class_="mlabels-right"):
        s.decompose()
    sig = clone.get_text(" ", strip=True)
    sig = re.sub(r"\(\s+", "(", sig)
    sig = re.sub(r"\s+\)", ")", sig)
    sig = re.sub(r"\s+,", ",", sig)
    sig = re.sub(r"\s{2,}", " ", sig).strip()
    # strip trailing qualifier labels that leak into the text (static, virtual, ...)
    for lab in reversed(labels):
        sig = re.sub(re.escape(lab) + r"\s*$", "", sig).strip()
    return sig, labels


# --------------------------------------------------------------------------- #
# DOM -> Markdown walker (prose + inline)
# --------------------------------------------------------------------------- #
class Renderer:
    def __init__(self, route_map: dict, anchor_map: dict, images: set):
        self.route_map = route_map
        self.anchor_map = anchor_map  # doxygen hash -> mintlify anchor (same page)
        self.images = images

    # ---- link / image helpers ------------------------------------------- #
    def rewrite_href(self, href: str) -> str | None:
        if not href:
            return None
        if href.startswith(("http://", "https://", "mailto:")):
            return href
        if href.startswith("#"):
            anchor = self.anchor_map.get(href[1:])
            return f"#{anchor}" if anchor else None
        base, _, frag = href.partition("#")
        route = self.route_map.get(base)
        if not route:
            return None
        return route  # cross-page member anchors don't map cleanly -> page top

    def rewrite_img(self, src: str) -> str:
        name = src.split("/")[-1]
        self.images.add(name)
        return f"{IMG_ROUTE}/{name}"

    # ---- inline ---------------------------------------------------------- #
    def inline(self, node) -> str:
        if isinstance(node, NavigableString):
            return esc(str(node))
        if not isinstance(node, Tag):
            return ""
        name = node.name
        if name in ("code", "tt") or (
            name == "span" and "computeroutput" in (node.get("class") or [])
        ):
            return f"`{node.get_text()}`"
        if name in ("b", "strong"):
            return f"**{self.inline_children(node).strip()}**"
        if name in ("i", "em"):
            inner = self.inline_children(node).strip()
            return f"*{inner}*" if inner else ""
        if name == "a":
            text = self.inline_children(node).strip()
            if text == "More..." or not text:
                return ""
            url = self.rewrite_href(node.get("href", ""))
            return f"[{text}]({url})" if url else text
        if name == "img":
            alt = esc(node.get("alt", ""))
            return f"![{alt}]({self.rewrite_img(node.get('src', ''))})"
        if name == "br":
            return "\n"
        return self.inline_children(node)

    def inline_children(self, node) -> str:
        return "".join(self.inline(c) for c in node.children)

    # ---- fenced code from a fragment ------------------------------------ #
    def fragment_code(self, node: Tag) -> str:
        lines = node.find_all("div", class_="line")
        if lines:
            code = "\n".join(ln.get_text() for ln in lines)
        else:
            code = node.get_text()
        code = code.rstrip()
        return f"\n```cpp\n{code}\n```\n"

    # ---- tables --------------------------------------------------------- #
    def render_named_list(self, tbl: Tag, name_cls: str, label: str) -> str:
        items = []
        for tr in tbl.find_all("tr"):
            ncell = tr.find("td", class_=name_cls)
            if not ncell:
                continue
            tds = tr.find_all("td")
            desc = self.inline_children(tds[-1]).strip() if len(tds) > 1 else ""
            nm = ncell.get_text(strip=True)
            items.append(f"- `{nm}`" + (f" — {desc}" if desc else ""))
        if not items:
            return ""
        return f"\n**{label}:**\n\n" + "\n".join(items) + "\n"

    def render_table(self, tbl: Tag) -> str:
        cls = tbl.get("class") or []
        if "params" in cls:
            return self.render_named_list(tbl, "paramname", "Parameters")
        if "exception" in cls:
            return self.render_named_list(tbl, "paramname", "Exceptions")
        if "fieldtable" in cls:
            return self.render_named_list(tbl, "fieldname", "Enumerator")
        # generic table
        rows = []
        for tr in tbl.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            rows.append(
                [self.inline_children(c).strip().replace("|", "\\|") for c in cells]
            )
        if not rows:
            return ""
        ncol = max(len(r) for r in rows)
        rows = [r + [""] * (ncol - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * ncol) + " |"]
        for r in rows[1:]:
            out.append("| " + " | ".join(r) + " |")
        return "\n" + "\n".join(out) + "\n"

    # ---- definition lists (Returns / Note / See also ...) --------------- #
    def render_dl(self, node: Tag) -> str:
        parts = []
        title = None
        for ch in node.children:
            if not isinstance(ch, Tag):
                continue
            if ch.name == "dt":
                title = self.inline_children(ch).strip()
            elif ch.name == "dd":
                body = self.block_children(ch).strip()
                # params/exception/enum tables carry their own label already
                has_labeled_table = ch.find(
                    "table", class_=["params", "exception", "fieldtable"]
                )
                if title and not has_labeled_table:
                    parts.append(f"**{title}:** {body}")
                else:
                    parts.append(body)
        return "\n\n" + "\n\n".join(p for p in parts if p) + "\n\n"

    # ---- block ---------------------------------------------------------- #
    def block(self, node, hbase: int = 1) -> str:
        if isinstance(node, NavigableString):
            t = str(node)
            return esc(t) if t.strip() else ""
        if not isinstance(node, Tag):
            return ""
        name = node.name
        classes = node.get("class") or []

        if name == "div" and "fragment" in classes:
            return self.fragment_code(node)
        if name == "pre":
            return f"\n```\n{node.get_text().rstrip()}\n```\n"
        if name in ("div",) and ("dynheader" in classes or "dyncontent" in classes):
            return ""  # inheritance / collaboration graphs
        if name == "div" and "caption" in classes:
            cap = self.inline_children(node).strip()
            return f"\n\n*{cap}*\n\n" if cap else ""
        if name == "img":
            alt = esc(node.get("alt", ""))
            return f"\n\n![{alt}]({self.rewrite_img(node.get('src', ''))})\n\n"
        if re.fullmatch(r"h[1-6]", name):
            level = min(6, int(name[1]) + hbase)
            text = self.inline_children(node).strip().lstrip("◆").strip()
            return f"\n\n{'#' * level} {text}\n\n"
        if name == "p":
            txt = self.block_children(node, hbase).strip()
            return f"\n{txt}\n" if txt else ""
        if name in ("ul", "ol"):
            return self.render_list(node, ordered=(name == "ol"))
        if name == "table":
            return self.render_table(node)
        if name == "dl":
            return self.render_dl(node)
        if name == "blockquote":
            inner = self.block_children(node, hbase).strip()
            return "\n" + "\n".join(f"> {ln}" for ln in inner.splitlines()) + "\n"
        if name in ("code", "tt", "b", "strong", "i", "em", "a", "img", "span", "br"):
            return self.inline(node)
        return self.block_children(node, hbase)

    def block_children(self, node, hbase: int = 1) -> str:
        return "".join(self.block(c, hbase) for c in node.children)

    def render_list(self, node: Tag, ordered: bool) -> str:
        out = []
        for i, li in enumerate(node.find_all("li", recursive=False), 1):
            marker = f"{i}." if ordered else "-"
            body = self.block_children(li).strip()
            body = body.replace("\n", "\n  ")
            out.append(f"{marker} {body}")
        return "\n" + "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Page builders
# --------------------------------------------------------------------------- #
def build_anchor_map(contents: Tag) -> dict:
    """Map doxygen member hash -> mintlify anchor slug for same-page links."""
    amap = {}
    for h in contents.find_all("h2", class_="memtitle"):
        a = h.find("a", id=True) or h.find_previous("a", id=True)
        name = h.get_text(strip=True).lstrip("◆").strip()
        if a and a.get("id"):
            amap[a["id"]] = mintlify_anchor(name)
    return amap


def frontmatter(title: str, sidebar: str, desc: str) -> str:
    def q(s: str) -> str:
        s = s.replace("\\", "").replace('"', "'").strip()
        s = re.sub(r"\s+", " ", s)
        return s

    lines = ["---", f'title: "{q(title)}"']
    if sidebar and sidebar != title:
        lines.append(f'sidebarTitle: "{q(sidebar)}"')
    if desc:
        lines.append(f'description: "{q(desc[:160])}"')
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def first_sentence(textblock: Tag) -> str:
    p = textblock.find("p")
    txt = (p.get_text(" ", strip=True) if p else textblock.get_text(" ", strip=True))
    m = re.match(r".+?[.!?](\s|$)", txt)
    return (m.group(0) if m else txt).strip()


def build_structured_page(soup, meta, route_map) -> str:
    contents = soup.find("div", class_="contents")
    anchor_map = build_anchor_map(contents)
    images: set = set()
    r = Renderer(route_map, anchor_map, images)

    body: list[str] = []
    desc = ""

    # top paragraphs: keep #include and "Inherits"; drop "Inherited by"
    textblock = None
    for ch in contents.children:
        if not isinstance(ch, Tag):
            continue
        if ch.name == "p":
            txt = ch.get_text(" ", strip=True)
            if txt.startswith("Inherited by"):
                continue
            if txt.startswith("#include"):
                body.append(f"`{txt}`\n")
            elif txt.startswith("Inherits"):
                body.append("\n" + r.block_children(ch).strip() + "\n")
        elif ch.name == "div" and "textblock" in (ch.get("class") or []):
            textblock = ch

    if textblock is not None:
        desc = first_sentence(textblock)
        body.append("\n## Overview\n")
        body.append(r.block_children(textblock).strip() + "\n")

    # member summary tables
    for tbl in contents.find_all("table", class_="memberdecls"):
        heading_tr = tbl.find("tr", class_="heading")
        section = (
            esc(re.sub(r"\s+", " ", heading_tr.get_text(" ", strip=True)))
            if heading_tr
            else "Members"
        )
        rows_md = render_memberdecls(tbl, r)
        if rows_md:
            body.append(f"\n## {section}\n")
            body.append(rows_md)

    # detail sections
    body.append(render_detail_sections(contents, r))

    md = frontmatter(meta["title"], meta["sidebar"], desc) + "\n".join(body)
    return normalize(md), images


def render_memberdecls(tbl: Tag, r: Renderer) -> str:
    items: list[str] = []
    pending = None
    for tr in tbl.find_all("tr"):
        cls = " ".join(tr.get("class") or [])
        if "inherit" in cls or "heading" in cls:
            continue
        left = tr.find("td", class_="memItemLeft")
        right = tr.find("td", class_="memItemRight")
        desc_right = tr.find("td", class_="mdescRight")
        if left is not None and right is not None:
            typ = r.inline_children(left).strip()
            nm = r.inline_children(right).strip()
            line = re.sub(r"\s+", " ", f"- {typ} {nm}").strip()
            items.append(line)
            pending = len(items) - 1
        elif desc_right is not None and pending is not None:
            d = re.sub(r"\s+", " ", r.inline_children(desc_right)).strip()
            if d:
                items[pending] += f" — {d}"
            pending = None
    return "\n".join(items) + "\n" if items else ""


def render_detail_sections(contents: Tag, r: Renderer) -> str:
    out: list[str] = []
    for el in contents.find_all("h2", class_="groupheader"):
        title = el.get_text(" ", strip=True)
        if title == "Detailed Description":
            continue
        if "Documentation" not in title:
            continue
        out.append(f"\n## {esc(title)}\n")
        # collect memitems until next groupheader
        for sib in el.find_all_next(["h2", "div"]):
            if sib.name == "h2" and "groupheader" in (sib.get("class") or []):
                break
            if sib.name == "div" and "memitem" in (sib.get("class") or []):
                out.append(render_memitem(sib, r))
    return "\n".join(out)


def member_heading(sig: str) -> str:
    """Derive a clean, MDX-safe member name from a signature.

    Cuts off argument lists ``(...)``, brace/equals initializers, then takes the
    trailing qualified identifier so brace-init default values never leak into a
    heading (raw ``{ }`` would break MDX).
    """
    decl = re.split(r"[({]", sig)[0]
    decl = decl.split("=")[0]
    tok = decl.split("::")[-1].strip()
    m = re.search(r"(~?[A-Za-z_]\w*|operator\S+)\s*$", tok) or re.search(
        r"(~?[A-Za-z_]\w*|operator\S+)\s*$", decl
    )
    heading = m.group(1) if m else tok
    heading = re.sub(r"[{}<>*]", "", heading).strip()
    return heading or "member"


def render_memitem(mi: Tag, r: Renderer) -> str:
    proto = mi.find("div", class_="memproto")
    doc = mi.find("div", class_="memdoc")
    sig, labels = clean_signature(proto) if proto else ("", [])
    heading = member_heading(sig)
    parts = [f"### {heading}"]
    if sig:
        parts.append(f"```cpp\n{sig}\n```")
    if labels:
        parts.append("*" + ", ".join(labels) + "*")
    if doc is not None:
        # strip reimplemented/implemented boilerplate
        for tag in doc.find_all(["p", "dl"]):
            t = tag.get_text(" ", strip=True)
            if t.startswith(REIMPL_PREFIXES):
                tag.decompose()
        body = r.block_children(doc).strip()
        if body:
            parts.append(body)
    return "\n\n" + "\n\n".join(parts) + "\n"


def build_prose_page(soup, meta, route_map, hbase=1) -> tuple[str, set]:
    contents = soup.find("div", class_="contents")
    images: set = set()
    r = Renderer(route_map, {}, images)
    # drop the duplicated page-title h1 in guides
    for h1 in contents.find_all("h1"):
        h1.decompose()
    desc = ""
    tb = contents.find("div", class_="textblock")
    if tb:
        desc = first_sentence(tb)
    body = r.block_children(contents, hbase).strip()
    md = frontmatter(meta["title"], meta["sidebar"], desc) + body
    return normalize(md), images


def normalize(md: str) -> str:
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #
def build_nav(pages_by_kind: dict) -> dict:
    def routes(kind):
        return sorted(
            f"{NAV_ROUTE_BASE}/{KIND_DIR[kind]}/{slug}"
            for _, slug in pages_by_kind.get(kind, [])
        )

    def alpha_groups(kind):
        entries = sorted(pages_by_kind.get(kind, []), key=lambda t: t[0].lower())
        groups: dict[str, list] = {}
        for sidebar, slug in entries:
            letter = sidebar[0].upper() if sidebar[:1].isalpha() else "#"
            groups.setdefault(letter, []).append(
                f"{NAV_ROUTE_BASE}/{KIND_DIR[kind]}/{slug}"
            )
        return [
            {"group": letter, "pages": sorted(pgs)}
            for letter, pgs in sorted(groups.items())
        ]

    api_pages = []
    if pages_by_kind.get("guide"):
        api_pages.append({"group": "Guides", "pages": routes("guide")})
    if pages_by_kind.get("namespace"):
        api_pages.append({"group": "Namespaces", "pages": routes("namespace")})
    if pages_by_kind.get("class"):
        api_pages.append({"group": "Classes", "pages": alpha_groups("class")})
    if pages_by_kind.get("struct"):
        api_pages.append({"group": "Structs", "pages": alpha_groups("struct")})
    if pages_by_kind.get("union"):
        api_pages.append({"group": "Unions", "pages": routes("union")})
    if pages_by_kind.get("example"):
        api_pages.append({"group": "Examples", "pages": routes("example")})
    return {"group": "API Reference", "pages": api_pages}


def update_docs_json(nav_group: dict):
    path = ROOT / "docs.json"
    data = json.loads(path.read_text())
    for product in data["navigation"]["products"]:
        if product.get("product") != "C++ SDK":
            continue
        for dd in product.get("dropdowns", []):
            if dd.get("dropdown") != "4.5":
                continue
            pages = [p for p in dd["pages"] if not (isinstance(p, dict) and p.get("group") == "API Reference")]
            pages.append(nav_group)
            dd["pages"] = pages
    path.write_text(json.dumps(data, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--no-nav", action="store_true")
    args = ap.parse_args()

    all_files = sorted(p.name for p in SRC.glob("*.html"))
    if args.only:
        selected = [(n, classify(n)) for n in args.only]
    else:
        selected = [(n, classify(n)) for n in all_files]
    selected = [(n, k) for n, k in selected if k]

    # Pass 1: build filename -> (meta, route) map
    meta_by_file: dict[str, dict] = {}
    route_map: dict[str, str] = {}
    used_slugs: dict[str, set] = {k: set() for k in KIND_DIR}
    for name, kind in selected:
        html = (SRC / name).read_text(encoding="utf-8", errors="replace")
        title = extract_title(html)
        if kind in ("class", "struct", "union", "namespace"):
            qual = clean_qualname(title)
            sidebar = qual.split("::")[-1] if "::" in qual else qual
            slug = slugify(qual)
        else:  # example / guide
            qual = title or name
            sidebar = qual
            slug = slugify(name[1:] if name.startswith("_") else name)
            slug = slug.replace("-example", "").replace("-html", "")
        base = slug
        i = 2
        while slug in used_slugs[kind]:
            slug = f"{base}-{i}"
            i += 1
        used_slugs[kind].add(slug)
        meta_by_file[name] = {"kind": kind, "title": qual or name, "sidebar": sidebar, "slug": slug}
        route_map[name] = f"{ROUTE_BASE}/{KIND_DIR[kind]}/{slug}"

    # Clean output dirs (idempotent) only on a full run
    full_run = not args.only and not args.limit
    if full_run:
        if OUT_BASE.exists():
            shutil.rmtree(OUT_BASE)
        if IMG_OUT.exists():
            shutil.rmtree(IMG_OUT)
    for k in KIND_DIR.values():
        (OUT_BASE / k).mkdir(parents=True, exist_ok=True)
    IMG_OUT.mkdir(parents=True, exist_ok=True)

    # Optional limit: keep a mix across kinds
    work = selected
    if args.limit:
        work = selected[: args.limit]

    pages_by_kind: dict[str, list] = {k: [] for k in KIND_DIR}
    all_images: set = set()
    errors = []
    for name, kind in work:
        meta = meta_by_file[name]
        try:
            soup = BeautifulSoup((SRC / name).read_text(encoding="utf-8", errors="replace"), "lxml")
            if kind in ("class", "struct", "union", "namespace"):
                md, imgs = build_structured_page(soup, meta, route_map)
            else:
                md, imgs = build_prose_page(soup, meta, route_map)
            out = OUT_BASE / KIND_DIR[kind] / f"{meta['slug']}.mdx"
            out.write_text(md, encoding="utf-8")
            all_images |= imgs
            pages_by_kind[kind].append((meta["sidebar"], meta["slug"]))
        except Exception as e:  # noqa: BLE001
            errors.append((name, repr(e)))

    # Copy referenced images
    copied = 0
    for img in all_images:
        src = SRC / img
        if src.exists():
            shutil.copy2(src, IMG_OUT / img)
            copied += 1

    total = sum(len(v) for v in pages_by_kind.values())
    print(f"Converted {total} pages ({', '.join(f'{k}={len(v)}' for k, v in pages_by_kind.items() if v)})")
    print(f"Copied {copied}/{len(all_images)} referenced images")
    if errors:
        print(f"\n{len(errors)} errors:")
        for n, e in errors[:20]:
            print(f"  {n}: {e}")

    if not args.no_nav and full_run:
        nav = build_nav(pages_by_kind)
        update_docs_json(nav)
        print("Updated docs.json navigation")


if __name__ == "__main__":
    main()
