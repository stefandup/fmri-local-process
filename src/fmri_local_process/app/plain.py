"""The window's help texts (Qt rich text) as plain text for a terminal."""

import html
import re

LINE_BREAK = re.compile(r"<br\s*/?>|</p>|</h\d>|</li>")
TAG = re.compile(r"<[^<>]+>")


def plain(text: str) -> str:
    return html.unescape(TAG.sub("", LINE_BREAK.sub("\n", text)))
