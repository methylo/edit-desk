#!/usr/bin/env python3
"""edit-desk 점검 도구. 표준 라이브러리만 쓰고 네트워크를 쓰지 않는다.

사용:
  python3 check.py --stats 원고.md                # 0·5단계 기초 수치 (문장·종결체·반복·상투어·병기·금지어)
  python3 check.py 원본.md 반영본.md              # 4단계 전후 대조 (문단 단위 + 숫자 보존 + 상투어 전후)
  python3 check.py 원본.md 반영본.md --sentence   # 범위 잠금: 문장 단위까지
  python3 check.py --apply 치환.json 원본.md 반영본.md  # 3단계 승인 문장 치환 후 전후 대조
  python3 check.py --voice                        # voice/ 파일 버전 줄 일치 확인

치환.json: [{"id": "수정 1", "old": "원문 문장", "new": "승인 문장"}, ...]
  old가 원본에 정확히 1회 나올 때만 치환한다. 하나라도 어긋나면 파일을 쓰지 않는다.

문단 번호는 frontmatter를 뺀 뒤 빈 줄 기준, 제목=0. --stats와 전후 대조가 같은 번호·같은 분량 기준을 쓴다.
한 줄이고 문장부호로 끝나지 않는 60자 이하 문단은 제목·소제목으로 보고 문장·종결 통계에서 뺀다.

기계 집계는 후보를 찾는 용도다. 위반 판정은 voice/03-anti-traces.md 기준으로 사람이 한다.
맞춤법·띄어쓰기는 이 도구가 보지 않는다.

종료코드: 0 통과 / 1 확인 필요
"""

import difflib
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
VOICE_DIR = SKILL_DIR / "voice"
VOICE_FILES = ["01-voice.md", "02-glossary.md", "03-anti-traces.md",
               "04-examples.md", "05-corrections.md"]
FORBIDDEN = ["혁신적", "획기적", "완벽한", "강력한", "효과적으로", "이를 통해", "결과적으로"]
QUANTIFIERS = ["대부분", "대다수", "모두", "전부", "늘 ", "항상", "언제나", "누구나", "아무도", "하루 종일", "절반", "거의"]
CONDITIONAL = ["매우", "쉽게", "빠르게", "상당히", "꽤", "할 수 있습니다", "할 수 있다"]
LONG_CHARS = 90
NUM_RE = re.compile(r"\d[\d,.]*\s*(?:%|퍼센트|배|명|개|년|월|일|시간|분|초|원|만|억|권|편|회|쪽|장|자|위|건|차시|기|주|살)?")
SKIP_LINE = re.compile(r"^\s*(#|>|\||```|- |\* |\d+\.\s|!\[|---|\[\[)")


# ---------- 공통 ----------

def strip_front(text):
    return re.sub(r"\A\ufeff?---\n.*?\n---\n", "", text, flags=re.S)


def paragraphs(text):
    """frontmatter를 뺀 뒤 빈 줄 기준 문단. 번호의 기준이다(제목=0)."""
    return [p.strip() for p in re.split(r"\n\s*\n", strip_front(text).strip()) if p.strip()]


def char_count(text):
    """분량: frontmatter 제외 전체 문단 글자 수. --stats와 전후 대조가 함께 쓴다."""
    return sum(map(len, paragraphs(text)))


def is_heading(p):
    """마크다운 기호 없는 평문 제목·소제목: 한 줄, 60자 이하, 문장부호로 끝나지 않음."""
    if "\n" in p or len(p) > 60:
        return False
    t = re.sub(r"[\"'”’)\]」』_*\s]+$", "", p)
    return not re.search(r"[.?!…]$", t)


def body_paragraphs(text, with_headings=False):
    """본문 문단만: 코드 블록·제목·목록·표·인용 제외. (원본 번호, 문단) 목록.
    번호는 paragraphs()와 같다. with_headings면 (본문, 평문 제목 번호 목록)을 돌려준다."""
    out, heads, in_code = [], [], False
    for n, p in enumerate(paragraphs(text)):
        fences = len(re.findall(r"^\s*```", p, flags=re.M))
        if in_code or fences:
            if fences % 2 == 1:
                in_code = not in_code
            continue
        lines = [l for l in p.splitlines() if not SKIP_LINE.match(l)]
        body = " ".join(l.strip() for l in lines).strip()
        if not body:
            continue
        if len(lines) == 1 and is_heading(body):
            heads.append(n)
            continue
        out.append((n, body))
    return (out, heads) if with_headings else out


def sentences(par):
    return [s.strip() for s in re.split(r"(?<=[.?!])\s+", par) if s.strip()]


def ending(s):
    t = re.sub(r"[\"'”’)\]」』\s]+$", "", s)
    if t.endswith("?"):
        return "의문"
    if t.endswith("!"):
        return "감탄"
    t = t.rstrip(".")
    if re.search(r"(니다|니까)$", t):
        return "합니다체"
    if re.search(r"요$", t):
        return "해요체"
    if re.search(r"다$", t):
        return "다체"
    return "기타"


def load_cliches():
    """voice/03-anti-traces.md §10 사전을 읽어 (범주, 표현) 목록을 만든다."""
    p = VOICE_DIR / "03-anti-traces.md"
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    m = re.search(r"## 10\..*?```text\n(.*?)```", text, re.S)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        parts = re.split(r"\s{2,}", line.strip(), maxsplit=1)
        if len(parts) != 2:
            continue
        cat, items = parts
        for it in items.split(" / "):
            it = it.strip().lstrip("~").strip()
            if not it or "N(" in it or "(" in it or "~" in it:
                continue
            out.append((cat, it))
    return out


def find_terms(text, terms):
    return {t: text.count(t) for t in terms if text.count(t)}


# ---------- --stats ----------

def stats(path):
    raw = Path(path).read_text(encoding="utf-8")
    paras, heads = body_paragraphs(raw, with_headings=True)
    sents = [(n, s) for n, p in paras for s in sentences(p)]
    if not sents:
        print("본문 문장 0개")
        return 1
    body = " ".join(p for _, p in paras)
    clen = [len(s) for _, s in sents]
    wlen = [len(s.split()) for _, s in sents]
    print(f"분량 {char_count(raw)}자 · 본문 문단 {len(paras)} · 문장 {len(sents)}")
    if heads:
        print("제목·소제목(문장·종결 통계 제외):", " ".join(f"[{n}]" for n in heads))
    longs = [f"[{n}] {len(s)}자 \"{s[:18]}…\"" for n, s in sents if len(s) >= LONG_CHARS]
    print(f"문장 길이(공백 포함) 중앙값 {statistics.median(clen):.0f}자 · 평균 {statistics.mean(clen):.1f}자"
          f" · {LONG_CHARS}자 이상 {sum(c >= LONG_CHARS for c in clen)}개"
          f"({sum(c >= LONG_CHARS for c in clen) / len(clen) * 100:.1f}%)")
    print(f"문장 길이(어절) 중앙값 {statistics.median(wlen):.0f} · 평균 {statistics.mean(wlen):.1f}")
    print(f"문단당 문장 평균 {len(sents) / len(paras):.1f}")
    if longs:
        print(f"{LONG_CHARS}자 이상 문장:", "; ".join(longs))

    ends = Counter(ending(s) for _, s in sents)
    print("종결:", " · ".join(f"{k} {v}({v / len(sents) * 100:.0f}%)" for k, v in ends.most_common()))
    main = max(("합니다체", "다체", "해요체"), key=lambda k: ends.get(k, 0))
    flag = 0
    mixed = []
    for n, p in paras:
        c = Counter(ending(s) for s in sentences(p))
        for k in ("합니다체", "다체", "해요체"):
            if k != main and c.get(k, 0) and c.get(k, 0) >= c.get(main, 0):
                mixed.append(f"[{n}]({k} {c[k]})")
    if mixed:
        flag = 1
        print(f"종결체 혼용 후보(주 종결: {main}):", ", ".join(mixed))

    # 같은 어두 반복: 문장 3연속, 문단 2연속
    reps = []
    firsts = [(n, s.split()[0]) for n, s in sents if s.split()]
    for i in range(len(firsts) - 2):
        w = {firsts[i][1], firsts[i + 1][1], firsts[i + 2][1]}
        if len(w) == 1:
            reps.append(f"문장 3연속 '{firsts[i][1]}' ([{firsts[i][0]}]~)")
    pf = [(n, p.split()[0]) for n, p in paras if p.split()]
    for i in range(len(pf) - 1):
        if pf[i][1] == pf[i + 1][1]:
            reps.append(f"문단 연속 시작 '{pf[i][1]}' ([{pf[i][0]}]·[{pf[i + 1][0]}])")
    # 비슷한 길이 3연속(±3자)
    for i in range(len(sents) - 2):
        a, b, c = clen[i:i + 3]
        if max(a, b, c) - min(a, b, c) <= 3 and sents[i][0] == sents[i + 2][0]:
            reps.append(f"비슷한 길이 3연속 {a}·{b}·{c}자 ([{sents[i][0]}])")
    if reps:
        print("리듬·반복 후보:", "; ".join(dict.fromkeys(reps)))

    qs = []
    for n, p in paras:
        for s_ in sentences(p):
            hit = [q.strip() for q in QUANTIFIERS if q in s_ + " "]
            if hit and not NUM_RE.search(s_):
                qs.append(f"[{n}] {'·'.join(hit)} \"{s_[:22]}…\"")
    if qs:
        print("수량어 후보(숫자 없는 양·빈도 단정, 문맥 판정):", "; ".join(qs[:12]))

    first = len(re.findall(r"(?:^|\s)(?:저는|제가|저도|제\s|나는|내가|나도|내\s)", body))
    if first >= 1:
        third = [f"[{n}] {w} \"{s_[:22]}…\"" for n, p in paras for s_ in sentences(p)
                 for w in ("사용자가", "사용자는", "작가가", "작가는", "필자는", "필자가") if w in s_]
        if third:
            print(f"인칭 섞임 후보(1인칭 표지가 있는 원고, 문맥 판정):", "; ".join(third[:10]))

    fb = find_terms(body, FORBIDDEN)
    cd = find_terms(body, CONDITIONAL)
    print("금지어", fb if fb else "0건")
    print("조건부(문맥 판정 필요)", cd if cd else "0건")
    flag |= bool(fb)

    cl = load_cliches()
    hits = Counter()
    where = {}
    for cat, t in cl:
        for n, p in paras:
            k = p.count(t)
            if k:
                hits[(cat, t)] += k
                where.setdefault((cat, t), []).append(n)
    total = sum(hits.values())
    print(f"상투어 후보(03 §10) {total}건 · 1,000자당 {total / len(body) * 1000:.1f}"
          f"  ※기계 집계, §11 지수와 다름")
    for (cat, t), k in hits.most_common(10):
        print(f"  {cat} | {t} {k}건 | {" ".join(f"[{n}]" for n in sorted(set(where[(cat, t)])))}")

    seen, miss = set(), []
    for n, p in paras:
        for m in re.finditer(r"[A-Za-z][A-Za-z0-9+#.\-]+(?:\s+[A-Za-z][A-Za-z0-9+#.\-]*)*", p):
            w = m.group(0).rstrip(".")
            if w.lower() in seen or re.match(r"https?|www", w):
                continue
            seen.add(w.lower())
            before = p[:m.start()]
            if before.rfind("(") <= before.rfind(")"):  # 괄호 안이면 병기로 본다
                miss.append(f"{w}[{n}]")
    if miss:
        print("영문 첫 등장 병기 확인 후보:", ", ".join(miss[:15]))

    nums = NUM_RE.findall(body)
    if nums:
        print("수치(사실 확인 대상):", ", ".join(dict.fromkeys(x.strip() for x in nums)))
    return flag


# ---------- 전후 대조 ----------

def compare(src, dst, sentence_level):
    ta = strip_front(Path(src).read_text(encoding="utf-8"))
    tb = strip_front(Path(dst).read_text(encoding="utf-8"))
    a, b = paragraphs(ta), paragraphs(tb)
    print("문단 대조 (원본 번호 기준, 제목=0)")
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if op == "equal":
            print(f"  동일  원본 {i1}~{i2 - 1}")
            continue
        label = {"replace": "변경", "delete": "삭제", "insert": "추가"}[op]
        print(f"  {label}  원본 {i1}~{i2 - 1} → 반영 {j1}~{j2 - 1}")
        if sentence_level and op == "replace" and (i2 - i1) == (j2 - j1):
            for k in range(i2 - i1):
                sa, sb = sentences(a[i1 + k]), sentences(b[j1 + k])
                diff = [n for n, (x, y) in enumerate(zip(sa, sb)) if x != y]
                if len(sa) != len(sb):
                    diff.append(f"문장 수 {len(sa)}→{len(sb)}")
                print(f"      문단 {i1 + k}: 바뀐 문장 {diff}")
    la, lb = sum(map(len, a)), sum(map(len, b))
    ratio = lb / la * 100 if la else 0
    print(f"분량 {la} → {lb}자 ({ratio:.1f}%)", "초과" if ratio > 120 else "")
    flag = ratio > 120

    text_b = "\n".join(b)
    hits = find_terms(text_b, FORBIDDEN)
    print("금지어(반영본)", hits if hits else "0건")
    flag |= bool(hits)

    na = Counter(x.strip() for x in NUM_RE.findall(ta))
    nb = Counter(x.strip() for x in NUM_RE.findall(tb))
    lost, added = na - nb, nb - na
    if lost or added:
        flag = True
        print("숫자 보존: 빠짐", dict(lost) or "-", "/ 새로 생김", dict(added) or "-",
              " ※ 승인 이슈로 설명되지 않으면 사실 훼손")
    else:
        print("숫자 보존: 일치")

    cl = load_cliches()
    if cl:
        ca = sum(ta.count(t) for _, t in cl)
        cb = sum(tb.count(t) for _, t in cl)
        print(f"상투어 후보(03 §10) {ca} → {cb}건 · 1,000자당 {ca / max(len(ta), 1) * 1000:.1f}"
              f" → {cb / max(len(tb), 1) * 1000:.1f}")
    old = {s for _, p in body_paragraphs(ta) for s in sentences(p)}
    new_long = [f"[{n}] {len(s)}자 \"{s[:18]}…\"" for n, p in body_paragraphs(tb)
                for s in sentences(p) if len(s) >= LONG_CHARS and s not in old]
    if new_long:
        print(f"{LONG_CHARS}자 이상 문장 새로 생김:", "; ".join(new_long),
              " ※ 나누거나 남기는 이유를 적는다")
    return 1 if flag else 0


# ---------- 승인 문장 치환 ----------

def apply(pairs_path, src, dst):
    if Path(src).resolve() == Path(dst).resolve():
        print("원본과 반영본 경로가 같습니다. 원본은 덮어쓰지 않습니다.")
        return 1
    pairs = json.loads(Path(pairs_path).read_text(encoding="utf-8"))
    text = Path(src).read_text(encoding="utf-8")
    bad = False
    for i, pr in enumerate(pairs, 1):
        label, c = pr.get("id", f"치환 {i}"), text.count(pr["old"])
        if pr["old"] == pr["new"]:
            print(f"{label}: 수정 전·후가 같습니다")
            bad = True
        elif c != 1:
            print(f"{label}: 원문에 {c}회 — 정확히 1회여야 합니다")
            bad = True
        else:
            print(f"{label}: 원문 1회 확인")
    out = text
    for i, pr in enumerate(pairs, 1):
        if bad:
            break
        if out.count(pr["old"]) != 1:
            print(f"{pr.get('id', f'치환 {i}')}: 앞선 치환과 구간이 겹칩니다. 한 쌍으로 합치십시오")
            bad = True
            break
        out = out.replace(pr["old"], pr["new"])
    if bad:
        print("치환하지 않았습니다. 파일을 쓰지 않았습니다.")
        return 1
    Path(dst).write_text(out, encoding="utf-8")
    print(f"치환 {len(pairs)}건 → {dst}")
    return compare(src, dst, False)


# ---------- voice ----------

def check_voice():
    lines = {}
    for name in VOICE_FILES:
        p = VOICE_DIR / name
        if not p.exists():
            print(f"없음: voice/{name}")
            return 1
        head = p.read_text(encoding="utf-8").splitlines()
        lines[name] = head[2] if len(head) > 2 else ""
    versions = {re.search(r"`(v[\d.]+)`", v).group(1) if re.search(r"`(v[\d.]+)`", v) else "?"
                for v in lines.values()}
    for name, v in lines.items():
        print(f"{name}: {v[:60]}")
    if len(versions) != 1:
        print("버전 불일치:", versions)
        return 1
    print("voice 버전 일치:", versions.pop())
    print(f"상투어 사전(03 §10) {len(load_cliches())}개 표현 읽음")
    return 0


if __name__ == "__main__":
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    if "--voice" in sys.argv:
        sys.exit(check_voice())
    if "--apply" in sys.argv and len(args) == 3:
        sys.exit(apply(args[0], args[1], args[2]))
    if "--stats" in sys.argv and len(args) == 1:
        sys.exit(stats(args[0]))
    if len(args) != 2:
        print(__doc__)
        sys.exit(1)
    sys.exit(compare(args[0], args[1], "--sentence" in sys.argv))
