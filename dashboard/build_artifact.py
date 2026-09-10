#!/usr/bin/env python3
"""
Builds inventory_artifact.html — the publishable variant of inventory.html.

Two differences from the repo file, both forced by how Artifacts are served:
  1. Fonts and the logo are inlined as base64 data URIs. The repo file points at
     ../Gilroy-*.woff and ../Elan - Wordmark - White.svg, which resolve only
     inside the repo; on the artifact origin they 404 and Gilroy silently falls
     back to system-ui.
  2. The document wrapper (<!doctype>, <html>, <head>, <body>) is stripped. The
     Artifact tool supplies its own, and a nested one breaks the page.

Run after build_inventory.py:
    python3 build_inventory.py
    python3 build_artifact.py
"""
import os
import re
import base64

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(HERE, "inventory.html")
OUT = os.path.join(HERE, "inventory_artifact.html")

FONTS = ["Regular", "Medium", "Semibold", "Bold", "Extrabold"]


def data_uri(path, mime):
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode("ascii")


def main():
    with open(SRC, encoding="utf-8") as f:
        html = f.read()

    # 1. Inline the Gilroy faces actually referenced by the stylesheet.
    for weight in FONTS:
        src = os.path.join(ROOT, f"Gilroy-{weight}.woff")
        if not os.path.exists(src):
            raise SystemExit(f"Missing font: {src}")
        uri = data_uri(src, "font/woff")
        old = f"url('../Gilroy-{weight}.woff')"
        if old not in html:
            raise SystemExit(f"Font reference not found in HTML: {old}")
        html = html.replace(old, f"url('{uri}')")

    # 2. Inline the wordmark.
    logo = os.path.join(ROOT, "Elan - Wordmark - White.svg")
    if not os.path.exists(logo):
        raise SystemExit(f"Missing logo: {logo}")
    logo_uri = data_uri(logo, "image/svg+xml")
    old_logo = 'src="../Elan%20-%20Wordmark%20-%20White.svg"'
    if old_logo not in html:
        raise SystemExit("Logo reference not found in HTML")
    html = html.replace(old_logo, f'src="{logo_uri}"')

    # 3. Drop the favicon link — the Artifact favicon is set at publish time.
    html = re.sub(r'\n?\s*<link rel="icon"[^>]*>', "", html)

    # 4. Unwrap: keep <title> + <style> from the head, and the body's contents.
    title = re.search(r"<title>.*?</title>", html, re.DOTALL)
    style = re.search(r"<style>.*?</style>", html, re.DOTALL)
    body = re.search(r"<body>\n?(.*)\n?</body>", html, re.DOTALL)
    if not (title and style and body):
        raise SystemExit("Could not unwrap the document — head/body markers not found")

    # The artifact title is its name in the gallery and browser tab, so it drops
    # the repo file's "Elan Clothing — " prefix and stands as a name on its own.
    out = ("<title>Elan Inventory Dashboard</title>\n"
           + style.group(0) + "\n\n" + body.group(1).strip() + "\n")

    for banned in ("<!doctype", "<html", "<head>", "<body>", "</html>"):
        if banned in out.lower():
            raise SystemExit(f"Wrapper tag survived unwrapping: {banned}")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"Wrote {OUT} ({len(out):,} bytes, {len(out)/1024/1024:.2f} MB)")


if __name__ == "__main__":
    main()
