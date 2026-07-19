"""
台灣縣市 + 學校分類資料

來源:教育部統計處(無公開 CSV mirror 可用時,用學制前綴 + 縣市對應規則)
更新日期:2026-07-19
資料筆數:22 縣市

注意:
- StudyArk response 沒有 county 欄位
- 縣市只能用 school_name 的前綴/關鍵字 substring match
- 因此「某縣市學校清單」其實是「常出現這個關鍵字的學校群」

例如:
- 「台北市」的學校可能叫:臺北市立○○、臺北市私立○○
- 但「中山國中」(沒前綴)可能是 22 個縣市都有的學校

這種情況我們用 *模糊匹配*:school_name 包含某個縣市的關鍵字就算。
"""

# 全 22 縣市 + 對應的常見關鍵字(用於從 school_name substring match)
# 順序按地理位置北→南、東→西

COUNTIES = [
    {"id": "taipei", "name": "臺北市", "keywords": ["臺北市", "台北市", "臺北市立", "台北市立", "臺北縣(舊)", "臺北"]},
    {"id": "new_taipei", "name": "新北市", "keywords": ["新北市", "新北市立", "新北", "臺北縣(舊)"]},
    {"id": "keelung", "name": "基隆市", "keywords": ["基隆市", "基隆市立", "基隆"]},
    {"id": "yilan", "name": "宜蘭縣", "keywords": ["宜蘭縣", "宜蘭縣立", "宜蘭"]},
    {"id": "taoyuan", "name": "桃園市", "keywords": ["桃園市", "桃園市立", "桃園"]},
    {"id": "hsinchu_city", "name": "新竹市", "keywords": ["新竹市", "新竹市立", "新竹市私立"]},
    {"id": "hsinchu_county", "name": "新竹縣", "keywords": ["新竹縣", "新竹縣立", "新竹"]},
    {"id": "miaoli", "name": "苗栗縣", "keywords": ["苗栗縣", "苗栗縣立", "苗栗"]},
    {"id": "taichung", "name": "臺中市", "keywords": ["臺中市", "台中市", "臺中市立", "台中市立", "臺中", "台中"]},
    {"id": "changhua", "name": "彰化縣", "keywords": ["彰化縣", "彰化縣立", "彰化"]},
    {"id": "nantou", "name": "南投縣", "keywords": ["南投縣", "南投縣立", "南投"]},
    {"id": "yunlin", "name": "雲林縣", "keywords": ["雲林縣", "雲林縣立", "雲林"]},
    {"id": "chiayi_city", "name": "嘉義市", "keywords": ["嘉義市", "嘉義市立", "嘉義市私立"]},
    {"id": "chiayi_county", "name": "嘉義縣", "keywords": ["嘉義縣", "嘉義縣立", "嘉義"]},
    {"id": "tainan", "name": "臺南市", "keywords": ["臺南市", "台南市", "臺南市立", "台南市立", "臺南", "台南"]},
    {"id": "kaohsiung", "name": "高雄市", "keywords": ["高雄市", "高雄市立", "高雄市私立", "高雄"]},
    {"id": "pingtung", "name": "屏東縣", "keywords": ["屏東縣", "屏東縣立", "屏東"]},
    {"id": "taitung", "name": "臺東縣", "keywords": ["臺東縣", "台東縣", "臺東縣立", "台東縣立", "臺東", "台東"]},
    {"id": "hualien", "name": "花蓮縣", "keywords": ["花蓮縣", "花蓮縣立", "花蓮"]},
    {"id": "penghu", "name": "澎湖縣", "keywords": ["澎湖縣", "澎湖縣立", "澎湖"]},
    {"id": "kinmen", "name": "金門縣", "keywords": ["金門縣", "金門縣立", "金門"]},
    {"id": "lienchiang", "name": "連江縣", "keywords": ["連江縣", "連江縣立", "連江", "馬祖"]},
]


def get_county_keywords(county_id: str) -> list[str]:
    """給 county id,回傳關鍵字 list"""
    for c in COUNTIES:
        if c["id"] == county_id:
            return c["keywords"]
    return []


def get_county_name(county_id: str) -> str:
    """給 county id,回傳中文名"""
    for c in COUNTIES:
        if c["id"] == county_id:
            return c["name"]
    return ""


def filter_school_by_county(school_name: str, county_id: str) -> bool:
    """
    判斷 school_name 是否屬於某 county。
    簡單 substring match:任何一個 county 關鍵字出現在 school_name 中就算。
    """
    if not county_id or county_id == "all":
        return True
    keywords = get_county_keywords(county_id)
    if not keywords:
        return True
    for kw in keywords:
        if kw in school_name:
            return True
    return False


# 學制對照(用 StudyArk grade 對應學制 level)
LEVEL_BY_GRADE = {
    "一年級": "國小",
    "二年級": "國小",
    "三年級": "國小",
    "四年級": "國小",
    "五年級": "國小",
    "六年級": "國小",
    "七年級": "國中",
    "八年級": "國中",
    "九年級": "國中",
    "高一": "高中",
    "高二": "高中",
    "高三": "高中",
}


def grade_to_level(grade: str | None) -> str | None:
    """年級 → 學制"""
    if not grade:
        return None
    return LEVEL_BY_GRADE.get(grade)