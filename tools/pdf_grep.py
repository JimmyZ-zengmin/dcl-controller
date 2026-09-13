"""
pdf_grep.py — 纯 stdlib 从 PDF 里抽文本并按关键词打印上下文窗口。

为什么自己写: 本机没有 pypdf/pdfminer, 而 RM0468 是**权威依据**(项目铁律: 先查
厂商权威记录再动手), 为一个查询装依赖不值得。RM 的正文是 FlateDecode 的
content stream, 用 zlib 解开后按 Tj/TJ 抽字符串即可 —— 对"查一段规格说明"够用。

用法:
  python pdf_grep.py <file.pdf> <kw1> [kw2 ...]        # 大小写不敏感, 打印 ±W 字符窗口
  python pdf_grep.py <file.pdf> --dump <out.txt>       # 导出全部文本供 grep
"""
import re
import sys
import zlib

W = 500  # 命中点前后窗口


def streams(data):
    pos = 0
    while True:
        i = data.find(b"stream", pos)
        if i < 0:
            return
        j = i + 6
        while j < len(data) and data[j] in (13, 10):
            j += 1
        k = data.find(b"endstream", j)
        if k < 0:
            return
        raw = data[j:k]
        pos = k + 9
        try:
            yield zlib.decompress(raw)
        except Exception:
            try:
                yield zlib.decompressobj().decompress(raw)
            except Exception:
                continue


def text_of(buf):
    out = []
    for m in re.finditer(rb"\((?:[^()\\]|\\.)*\)", buf):
        s = m.group(0)[1:-1]
        s = s.replace(b"\\(", b"(").replace(b"\\)", b")")
        s = s.replace(b"\\\\", b"\\")
        out.append(s)
    t = b"".join(out)
    return re.sub(rb"\s+", b" ", t)


def main():
    path = sys.argv[1]
    data = open(path, "rb").read()
    texts = []
    for s in streams(data):
        if b"Tj" in s or b"TJ" in s:
            t = text_of(s)
            if t:
                texts.append(t)
    big = b"\n".join(texts)
    sys.stderr.write("streams=%d text=%d bytes\n" % (len(texts), len(big)))

    if sys.argv[2] == "--dump":
        open(sys.argv[3], "wb").write(big)
        return

    for kw in sys.argv[2:]:
        k = kw.encode().lower()
        seen = set()
        n = 0
        for m in re.finditer(re.escape(k), big.lower()):
            a = max(0, m.start() - W)
            b = min(len(big), m.end() + W)
            snippet = big[a:b].decode("latin-1")
            key = snippet[:80]
            if key in seen:
                continue
            seen.add(key)
            n += 1
            print("=" * 78)
            print("[%s] hit %d" % (kw, n))
            print(snippet.replace("  ", " "))
            if n >= 12:
                print("... (more hits suppressed)")
                break


main()
