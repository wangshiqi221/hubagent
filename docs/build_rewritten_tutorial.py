"""Assemble tutorial and browser-clipboard HTML; preserve application source."""
from pathlib import Path
import hashlib
import html
import re

root = Path(__file__).resolve().parents[1]
docs = root / 'docs'
body = (docs / 'Agent教程_重写正文.md').read_text(encoding='utf-8')
appendix = (docs / '2.2_Agent核心代码逐项解读.md').read_text(encoding='utf-8')
appendix = appendix[appendix.index('##### ①'):]
counter = 0
adjusted = []
for line in appendix.splitlines():
    if line.startswith('##### '):
        counter += 1
        title = re.sub(r'^[①-⑳]\s*', '', line[6:])
        line = f'### B.{counter} {title}'
    adjusted.append(line)
text = body.replace('@@SOURCE_APPENDIX@@', '\n'.join(adjusted))
assert '@@SOURCE_APPENDIX@@' not in text
assert text.count('```') % 2 == 0
assert len(re.findall(r'^## 第[一二三四五六七八]章', text, flags=re.M)) == 8
(docs / '轮辋逆向重建Agent_从原理到复现.md').write_text(text, encoding='utf-8')

def inline(s):
    chunks = re.split(r'(`[^`]+`)', s)
    out = []
    for c in chunks:
        if c.startswith('`') and c.endswith('`'):
            out.append('<code>' + html.escape(c[1:-1]) + '</code>')
        else:
            z = html.escape(c)
            z = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', z)
            out.append(z)
    return ''.join(out)

# A small renderer for this document's headings, fenced code, tables, lists and prose.
lines = text.splitlines()
out = []
i = 0
while i < len(lines):
    line = lines[i]
    if line.startswith('```'):
        lang = line[3:].strip()
        code = []
        i += 1
        while i < len(lines) and not lines[i].startswith('```'):
            code.append(lines[i]); i += 1
        out.append('<pre style="white-space:pre"><code class="language-' + html.escape(lang) + '">' + html.escape('\n'.join(code)) + '</code></pre>')
    elif re.match(r'^#{1,6} ', line):
        n = len(line)-len(line.lstrip('#'))
        out.append(f'<h{n}>' + inline(line[n+1:]) + f'</h{n}>')
    elif line.startswith('|'):
        rows = []
        while i < len(lines) and lines[i].startswith('|'):
            cells = lines[i].strip().strip('|').split('|')
            if not all(re.fullmatch(r'\s*:?-+:?\s*', c) for c in cells):
                tag = 'th' if not rows else 'td'
                rows.append('<tr>' + ''.join(f'<{tag}>'+inline(c.strip())+f'</{tag}>' for c in cells) + '</tr>')
            i += 1
        out.append('<table border="1"><tbody>'+''.join(rows)+'</tbody></table>')
        continue
    elif re.match(r'^(- |\d+\. )', line):
        ordered = bool(re.match(r'^\d+\. ', line))
        tag = 'ol' if ordered else 'ul'
        items = []
        pattern = r'^\d+\. ' if ordered else r'^- '
        while i < len(lines) and re.match(pattern, lines[i]):
            items.append('<li>'+inline(re.sub(pattern,'',lines[i]))+'</li>'); i += 1
        out.append(f'<{tag}>'+''.join(items)+f'</{tag}>')
        continue
    elif line.strip():
        if line.startswith('> '):
            out.append('<blockquote><p>'+inline(line[2:])+'</p></blockquote>')
        else:
            out.append('<p>'+inline(line)+'</p>')
    i += 1
(docs / '轮辋逆向重建Agent_重写版.html').write_text('<html><body>'+''.join(out)+'</body></html>', encoding='utf-8')
sources = ['agent/loop.py','agent/rim_pipeline.py','agent/tool_defs.py','agent/prompts.py','core/llm_client.py','core/session.py','ui/panel.py']
(docs / '教程源码核对_SHA256.txt').write_text('\n'.join(hashlib.sha256((root/p).read_bytes()).hexdigest()+'  '+p for p in sources)+'\n',encoding='utf-8')
print('Built eight-chapter tutorial:',len(text),'characters;',counter,'appendix entries; balanced fences.')
