"""두 보충 매뉴얼의 정적 웹 페이지 생성. 외부 패키지/네트워크 불필요.

현재 문서에서 쓰는 제목, 문단, 단일 단계 목록, 표, 코드/강조, HTTPS 링크만 지원한다.
지원하지 않는 표현은 텍스트로 보존하며 HTML은 항상 escape한다.
"""
from pathlib import Path
import argparse
import html
import re

ROOT = Path(__file__).resolve().parents[2]
MANUALS = {
    "cga-alone-guide-ko.html": ROOT / "docs/manual/CGA_ALONE_DEVELOPMENT_GUIDE.md",
    "channel-connections-ko.html": ROOT / "docs/manual/channel-connections.md",
}


def inline(text):
    tokens = re.split(r"(`[^`]+`|\[[^\]]+\]\(https://[^\s)]+\)|\*\*[^*]+\*\*)", text)
    result = []
    for token in tokens:
        if token.startswith("`") and token.endswith("`"):
            result.append("<code>" + html.escape(token[1:-1]) + "</code>")
        elif token.startswith("**") and token.endswith("**"):
            result.append("<strong>" + html.escape(token[2:-2]) + "</strong>")
        elif match := re.fullmatch(r"\[([^\]]+)\]\((https://[^\s)]+)\)", token):
            result.append('<a href="' + html.escape(match[2], quote=True) + '" rel="noreferrer">' + html.escape(match[1]) + '</a>')
        else:
            result.append(html.escape(token))
    return "".join(result)


def render_markdown(text):
    lines = text.splitlines()
    output = []
    i = 0
    section = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if match := re.match(r"^(#{1,3}) (.+)$", line):
            level = len(match[1])
            ident = ''
            if level == 2:
                section += 1
                ident = f' id="section-{section}"'
            output.append(f'<h{level}{ident}>{inline(match[2])}</h{level}>')
            i += 1
        elif line.startswith('|') and i + 1 < len(lines) and re.fullmatch(r"[| :\-]+", lines[i+1].strip()):
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                rows.append([cell.strip() for cell in lines[i].strip().strip('|').split('|')])
                i += 1
            header = ''.join('<th scope="col">' + inline(cell) + '</th>' for cell in rows[0])
            body = ''.join('<tr>' + ''.join('<td>' + inline(cell) + '</td>' for cell in row) + '</tr>' for row in rows[2:])
            output.append('<div class="table-scroll"><table><thead><tr>' + header + '</tr></thead><tbody>' + body + '</tbody></table></div>')
        elif re.match(r"^(?:- |\d+\. )", line):
            ordered = bool(re.match(r"^\d+\. ", line))
            pattern = r"^\d+\. (.+)$" if ordered else r"^- (.+)$"
            tag = 'ol' if ordered else 'ul'
            items = []
            while i < len(lines) and (match := re.match(pattern, lines[i].strip())):
                items.append('<li>' + inline(match[1]) + '</li>')
                i += 1
            output.append(f'<{tag}>' + ''.join(items) + f'</{tag}>')
        else:
            output.append('<p>' + inline(line) + '</p>')
            i += 1
    return '\n'.join(output)


CSS = """*{box-sizing:border-box}html{scroll-padding-top:24px}body{margin:0;background:#f3f5f8;color:#24344c;font:16px/1.8 system-ui,'Malgun Gothic',sans-serif}main{max-width:78ch;margin:32px auto;padding:32px;background:#fff;border:1px solid #d8dde3}h1{font-size:1.8rem;line-height:1.35;margin:16px 0 24px}h2{font-size:1.35rem;margin:40px 0 12px;line-height:1.5}h3{font-size:1.1rem;margin:28px 0 8px}p{margin:12px 0}li{margin:8px 0}a{color:#245888;text-underline-offset:3px}a:hover{color:#173e64}a:focus-visible{outline:2px solid #245888;outline-offset:4px}code{font-size:.9em;overflow-wrap:anywhere}p,li,td{overflow-wrap:anywhere}.table-scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{padding:8px 12px;border:1px solid #d8dde3;text-align:left}th{background:#f3f5f8}nav ul{padding-left:24px}header{border-bottom:1px solid #d8dde3;padding-bottom:16px}::selection{background:#c8e4fa;color:#172a42}@media(max-width:600px){main{margin:0;padding:20px;border:0}h1{font-size:1.75rem}}@media print{body{background:#fff}main{border:0;margin:0;max-width:none}header,nav{display:none}}"""


def build_page(source):
    text = source.read_text(encoding='utf-8')
    title = text.splitlines()[0].removeprefix('# ')
    headings = re.findall(r'^## (.+)$', text, re.MULTILINE)
    toc = ''.join(f'<li><a href="#section-{i}">{html.escape(label)}</a></li>' for i, label in enumerate(headings, 1))
    return ('<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>' + html.escape(title) + ' | CGA-alone 도움말</title><style>' + CSS + '</style></head><body><main>'
            '<header><a href="cga-alone-guide-ko.html">CGA-alone 기능 안내</a> · '
            '<a href="channel-connections-ko.html">채널 연결 관리</a><p>한국어 보충 매뉴얼 · 기존 CGA PDF와 함께 읽으세요.</p></header>'
            + '<h1>' + html.escape(title) + '</h1><nav aria-label="목차"><details><summary>목차</summary><ul>' + toc + '</ul></details></nav>'
            + render_markdown('\n'.join(text.splitlines()[1:])) + '</main></body></html>\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='문서와 웹 산출물의 일치 여부만 확인')
    args = parser.parse_args()
    for name, source in MANUALS.items():
        target = ROOT / 'apps/web/public/manuals' / name
        expected = build_page(source)
        if args.check:
            if not target.exists() or target.read_text(encoding='utf-8') != expected:
                raise SystemExit(f'문서 재생성이 필요합니다: {name}')
        else:
            target.write_text(expected, encoding='utf-8', newline='\n')
    print('보충 매뉴얼 2개 ' + ('일치 확인' if args.check else '웹 생성 완료'))
