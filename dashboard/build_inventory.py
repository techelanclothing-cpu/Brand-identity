#!/usr/bin/env python3
"""
Embeds dashboard/inventory_data.json into the <script id="inventory-data">
block in inventory.html, so the dashboard is a single self-contained file.

Run after build_inventory_data.py:
    python3 build_inventory_data.py <raw-dir>
    python3 build_inventory.py
"""
import os
import re
import json

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, "inventory.html")
DATA_PATH = os.path.join(HERE, "inventory_data.json")

PATTERN = re.compile(
    r'(<script id="inventory-data" type="application/json">\n)(.*?)(\n</script>)',
    re.DOTALL,
)


def main():
    with open(DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)  # validates it's well-formed JSON
    # Compact: this payload carries every variant, so keep the file small.
    data_text = json.dumps(data, separators=(",", ":"))
    # `</script>` inside a JSON string would close the block early.
    data_text = data_text.replace("</", "<\\/")

    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()

    new_html, count = PATTERN.subn(lambda m: m.group(1) + data_text + m.group(3), html)
    if count != 1:
        raise SystemExit(
            "Could not find the inventory-data script block in inventory.html "
            "(expected exactly one match, found %d)" % count
        )

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)
    print(f"Embedded {DATA_PATH} into {HTML_PATH} ({len(data_text)} bytes of data)")


if __name__ == "__main__":
    main()
