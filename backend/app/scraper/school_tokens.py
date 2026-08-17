"""學校名核心 token 化 (2026-08-17 新)
拆 dropdown value 成核心 token, DB 用 LIKE '%token%' AND 全部 match
例: "高雄市楠梓國中" → ["楠梓"]
DB "高雄市立楠梓國中" 也 → ["楠梓"] → match
"""
import re

_COUNTY_PREFIXES = [
    "臺北市", "台北市", "新北市", "桃園市", "臺中市", "台中市",
    "臺南市", "台南市", "高雄市", "基隆市", "新竹市", "新竹縣",
    "宜蘭縣", "苗栗縣", "彰化縣", "南投縣", "雲林縣", "嘉義市",
    "嘉義縣", "屏東縣", "花蓮縣", "臺東縣", "台東縣", "澎湖縣",
    "金門縣", "連江縣",
]
_SCHOOL_SUFFIXES = [
    "國民中學", "高級中學", "國民小學", "國中", "國小", "高中", "高職",
]

def core_tokens(school_name: str) -> list:
    """拆學校名成核心 token list (AND 搜尋用).
    
    "高雄市楠梓國中" → ["楠梓"]
    "高雄市立楠梓國中" → ["楠梓"]
    "楠梓國中" → ["楠梓"]
    "臺北市立建國高級中學" → ["建國"]
    """
    if not school_name:
        return []
    s = school_name.strip()
    # 去 county prefix
    for prefix in _COUNTY_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # 去 leading 立
    if s.startswith("立"):
        s = s[1:]
    # 去 suffix (從長到短)
    for suffix in sorted(_SCHOOL_SUFFIXES, key=len, reverse=True):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    s = s.strip()
    if len(s) >= 2:
        return [s]
    return []

if __name__ == "__main__":
    tests = [
        ("高雄市楠梓國中", ["楠梓"]),
        ("高雄市立楠梓國中", ["楠梓"]),
        ("楠梓國中", ["楠梓"]),
        ("臺北市立建國高級中學", ["建國"]),
        ("臺北市私立大同高中", ["大同"]),
        ("臺中市立新民高級中學", ["新民"]),
        ("", []),
        ("高", []),
        ("高雄", []),
        ("高雄市立", []),
    ]
    for input_, expected in tests:
        got = core_tokens(input_)
        ok = "✓" if got == expected else "✗"
        print(f"  {ok} core_tokens({input_!r}) = {got} (expected {expected})")
