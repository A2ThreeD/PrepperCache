# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Check standalone and inline JavaScript without starting a browser.

from html.parser import HTMLParser
from pathlib import Path
import subprocess


class InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and "src" not in attributes and not attributes.get("type"):
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None


for path in sorted(Path("web").rglob("*.js")):
    subprocess.run(["node", "--check", str(path)], check=True)

for path in sorted(Path("web").rglob("*.html")):
    parser = InlineScripts()
    parser.feed(path.read_text(encoding="utf-8"))
    for script in parser.scripts:
        result = subprocess.run(["node", "--check", "-"], input=script, text=True)
        if result.returncode:
            raise SystemExit(f"Invalid inline JavaScript in {path}")

print("Shell and JavaScript syntax checks passed.")
