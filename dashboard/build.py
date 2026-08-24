#!/usr/bin/env python3
"""
Embeds dashboard/data.json into the <script id="dashboard-data"> block in
index.html, so the dashboard works as a single static file (no fetch(), no
CORS issues when opened directly or hosted anywhere).

Run this after fetch_metrics.py refreshes data.json:
    python3 fetch_metrics.py
    python3 build.py
"""
import os
import re
import json

HERE = os.path.dirname(__file__)
HTML_PATH = os.path.join(HERE, "index.html")
DATA_PATH = os.path.join(HERE, "data.json")

PATTERN = re.compile(
    r'(<script id="dashboard-data" type="application/json">\n)(.*?)(\n</script>)',
    re.DOTALL,
)


def main():
    with open(DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)  # validates it's well-formed JSON
    data_text = json.dumps(data, indent=2)

    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()

    new_html, count = PATTERN.subn(lambda m: m.group(1) + data_text + m.group(3), html)
    if count != 1:
        raise SystemExit(
            "Could not find the dashboard-data script block in index.html "
            "(expected exactly one match, found %d)" % count
        )

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)
    print(f"Embedded {DATA_PATH} into {HTML_PATH}")


if __name__ == "__main__":
    main()
