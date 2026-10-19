"""Markdown to inert terminal style fragments; never HTML, ANSI or link actions."""
from __future__ import annotations

from functools import lru_cache
import re


def safe_text(value):
    """Do not let remote text drive terminal escapes or bidi controls."""
    text = str(value)
    text = re.sub(r"\x1b\](?:(?!\x07|\x1b\\).)*(?:\x07|\x1b\\|$)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b.", "", text, flags=re.DOTALL)
    return "".join(c for c in text if (c in "\n\t" or ord(c) >= 32)
                   and not 0x7f <= ord(c) <= 0x9f and not 0x202a <= ord(c) <= 0x202e
                   and not 0x2066 <= ord(c) <= 0x2069 and c not in "\u061c\u200e\u200f" and not 0xd800 <= ord(c) <= 0xdfff)


def _relaxed_strong(state, silent):
    """Accept model output such as **planner（我） ** outside code/escapes.

    Only a single-line, otherwise paired ** span with trailing whitespace is
    extended. Normal CommonMark handles all other emphasis, including nesting.
    Unclosed delimiters remain visible while streaming; no text is guessed away.
    """
    start = state.pos
    if silent or not state.src.startswith("**", start) or state.src.startswith("***", start):
        return False
    if start and state.src[start - 1] == "*":
        return False
    end = state.src.find("**", start + 2, state.posMax)
    if end < 0 or end + 2 < state.posMax and state.src[end + 2] == "*":
        return False
    body = state.src[start + 2:end]
    if (not body or body[0].isspace() or body[-1] not in " \t" or "\n" in body
            or any(marker in body for marker in "`\\[]")):
        # This compatibility extension is only for plain labels. A closing **
        # found inside code, escapes or links is not a real emphasis delimiter;
        # leave those cases to the full CommonMark parser.
        return False
    token = state.push("relaxed_strong", "strong", 0)
    token.children = []
    state.md.inline.parse(body, state.md, state.env, token.children)
    state.pos = end + 2
    return True


@lru_cache(maxsize=1)
def _parser():
    from markdown_it import MarkdownIt
    parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    parser.inline.ruler.before("emphasis", "relaxed_strong", _relaxed_strong)
    return parser


class _Writer:
    def __init__(self):
        self.fragments = []
        self.trailing_newlines = 0
        self.line_start = True
        self.quote_depth = 0
        self.lists = []

    def raw(self, style, text):
        # Parsing entities can introduce controls after the initial sanitizing.
        text = safe_text(text)
        if not text:
            return
        self.fragments.append((style, text))
        if text.endswith("\n"):
            count = len(text) - len(text.rstrip("\n"))
            self.trailing_newlines = self.trailing_newlines + count if count == len(text) else count
            self.line_start = True
        else:
            self.trailing_newlines = 0
            self.line_start = False

    def emit(self, text, style="", *, indent=None):
        for part in text.splitlines(keepends=True):
            if self.line_start and part != "\n":
                prefix = "│ " * self.quote_depth + "  " * (len(self.lists) if indent is None else indent)
                self.raw("class:md.quote", prefix)
            self.raw(style, part)

    def newline(self, count=1):
        if self.fragments and self.trailing_newlines < count:
            self.raw("", "\n" * (count - self.trailing_newlines))

    def inline(self, tokens, base=""):
        styles = [base]
        links = []
        opened = {"strong_open": "class:md.bold", "em_open": "class:md.italic", "s_open": "class:md.strike"}
        for token in tokens or []:
            kind = token.type
            style = " ".join(s for s in styles if s)
            if kind in opened:
                styles.append(opened[kind])
            elif kind in {"strong_close", "em_close", "s_close"}:
                styles.pop()
            elif kind == "relaxed_strong":
                self.inline(token.children, style + " class:md.bold")
            elif kind == "code_inline":
                self.emit(token.content, style + " class:md.code")
            elif kind in {"softbreak", "hardbreak"}:
                self.raw("", "\n")
            elif kind == "link_open":
                links.append((safe_text(token.attrGet("href") or ""), len(self.fragments)))
                styles.append("class:md.link")
            elif kind == "link_close":
                styles.pop()
                url, start = links.pop()
                label = "".join(text for _, text in self.fragments[start:])
                if url and url != label:
                    self.emit(" (" + url + ")", "class:md.link")
            elif kind == "image":
                self.emit("[圖片：" + safe_text(token.content) + "]", style)
                url = safe_text(token.attrGet("src") or "")
                if url:
                    self.emit(" (" + url + ")", "class:md.link")
            else:
                self.emit(token.content, style)


@lru_cache(maxsize=32)
def render_markdown(text):
    """Return immutable (style, text) pairs; core/plain installs can fall back."""
    clean = safe_text(text)
    try:
        tokens = _parser().parse(clean)
    except ImportError:
        return (("", clean),)
    writer = _Writer()
    heading = False
    table_head = False
    table_cell = 0
    for token in tokens:
        kind = token.type
        if kind == "inline":
            writer.inline(token.children, "class:md.heading" if heading else "class:md.bold" if table_head else "")
        elif kind == "heading_open":
            writer.newline(2)
            heading = True
        elif kind == "heading_close":
            writer.newline(2)
            heading = False
        elif kind == "paragraph_close":
            writer.newline(1 if token.hidden else 2)
        elif kind in {"bullet_list_open", "ordered_list_open"}:
            writer.newline(1 if writer.lists else 2)
            writer.lists.append({"ordered": kind == "ordered_list_open", "next": int(token.attrGet("start") or 1)})
        elif kind == "list_item_open":
            writer.newline()
            item = writer.lists[-1]
            marker = str(item["next"]) + ". " if item["ordered"] else "• "
            item["next"] += 1
            writer.emit(marker, "class:md.marker", indent=len(writer.lists) - 1)
        elif kind == "list_item_close":
            writer.newline()
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            writer.lists.pop()
            writer.newline(1 if writer.lists else 2)
        elif kind == "blockquote_open":
            writer.newline()
            writer.quote_depth += 1
        elif kind == "blockquote_close":
            writer.quote_depth -= 1
            writer.newline(2)
        elif kind in {"fence", "code_block"}:
            writer.newline()
            if token.info.strip():
                writer.emit(token.info.strip() + "\n", "class:md.code-label")
            writer.emit(token.content, "class:md.code")
            writer.newline(2)
        elif kind == "hr":
            writer.newline()
            writer.emit("────────────────────", "class:md.marker")
            writer.newline(2)
        elif kind == "thead_open":
            table_head = True
        elif kind == "thead_close":
            table_head = False
        elif kind == "tr_open":
            writer.newline()
            table_cell = 0
        elif kind in {"th_open", "td_open"}:
            if table_cell:
                writer.emit(" │ ", "class:md.marker")
            table_cell += 1
        elif kind == "tr_close":
            writer.newline()
        elif kind == "table_close":
            writer.newline(2)
        elif token.content:
            writer.emit(token.content)
    # Keep block separation internally without adding a trailing blank block.
    while writer.fragments and not writer.fragments[-1][1].strip("\n"):
        writer.fragments.pop()
    return tuple(writer.fragments)


def clip_fragments(fragments, limit=100000):
    """Keep text and styles aligned when limiting the visible transcript tail."""
    remaining = limit
    tail = []
    for style, text in reversed(fragments):
        if remaining <= 0:
            break
        text = text[-remaining:]
        tail.append((style, text))
        remaining -= len(text)
    return tuple(reversed(tail))
