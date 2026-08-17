// =========================================================
// dashboard form 互動邏輯 (2026-07-24)
// 監聽 grade dropdown 變化, 動態切換:
//   1. 科目清單 (StudyArk / CAP / CEEC)
//   2. 隱藏無關 filter (縣市 / 學校 / 學期 / 段考 / 版本 / 含答案)
//   3. 隱藏 StudyArk 相關說明文字 + 限流警語
//   4. form action / 按鈕文字 / card title
//
// 規則:
//   - 選擇「歷屆會考」 (value="會考")
//     → 顯示 CAP 科目清單 (從後端拿真實清單, 含「寫作測驗」)
//     → 隱藏 StudyArk-only 元素 (.studyark-only)
//     → 學年 保留 (讓 user 篩選特定年度)
//     → 按鈕變成「🔍 搜尋 歷屆會考」
//
//   - 選擇「歷屆大學入學考」 (value="大學入學考")
//     → 顯示 CEEC 科目清單 (從後端拿真實清單)
//     → 隱藏 StudyArk-only 元素
//     → 學年 保留
//     → 按鈕變成「🔍 搜尋 歷屆大學入學考」
//
//   - 選擇一般年級 (一年級 ~ 高三)
//     → 顯示 StudyArk 科目清單
//     → 顯示所有 StudyArk-only 元素
//     → form action = /ui/search, 按鈕 = 「🔍 搜尋」
// =========================================================

// 【2026-07-24 改】CAP/CEEC subjects 從後端 Jinja 注入, 不再 hardcode
//   為的是 CAP 有「寫作測驗」、CEEC 有「國語文綜合能力測驗」等特殊科目
//   後端從 _scan_pdf_tree 抓真實清單, 這裡只 fallback 到 hardcoded list
const SUBJECTS_FALLBACK = {
    cap: ["國文", "英語", "數學", "社會", "自然", "寫作測驗", "參考答案", "其他", "試題說明"],
    ceec: [
        "國文", "國綜", "國寫", "國語文綜合能力測驗", "國語文寫作能力測驗",
        "英文", "數學", "數甲", "數乙", "數a", "數b", "數學a", "數學b", "數學甲", "數學乙",
        "社會", "自然", "物理", "化學", "生物", "歷史", "地理", "公民", "公民與社會",
    ],
    studyark: [
        "數學", "國語", "英語", "生活", "健康與體育",
        "社會", "地理", "歷史", "理化", "公民", "自然", "作文",
    ],
};

function getSubjects(mode) {
    const win = window.DASHBOARD_SUBJECTS || {};
    const fromServer = win[mode] || [];
    if (fromServer.length > 0) return fromServer;
    return SUBJECTS_FALLBACK[mode] || [];
}

// 【2026-08-17 新】CAP/CEEC 學年清單 (從後端傳來的 window.DASHBOARD_YEARS)
function getYears(mode) {
    const win = window.DASHBOARD_YEARS || {};
    return win[mode] || [];
}

// 【2026-08-17 改】學年 select dropdown 重建 (DESC 排序)
function populateYears(selectEl, years, currentValue) {
    if (!selectEl) return;
    const defaultOpt = selectEl.options[0];
    selectEl.innerHTML = "";
    if (defaultOpt) selectEl.appendChild(defaultOpt);
    years.forEach(y => {
        const o = document.createElement("option");
        o.value = String(y);
        o.textContent = String(y);
        selectEl.appendChild(o);
    });
    if (currentValue && Array.from(selectEl.options).some(o => o.value === String(currentValue))) {
        selectEl.value = String(currentValue);
    } else {
        selectEl.value = "";
    }
}

// =========================================================
// State
// =========================================================
let currentMode = "studyark"; // studyark | cap | ceec
let savedValues = {
    subject: "",
    county: "",
    school_name: "",
    school_year: "",
    school_term: "",
    exam_type: "",
    version: "",
    daan: "",
};

// =========================================================
// DOM refs
// =========================================================
function $id(id) { return document.getElementById(id); }
function $all(sel) { return Array.from(document.querySelectorAll(sel)); }

let form, gradeSelect, subjectSelect, countySelect, schoolNameSelect, submitBtn, cardTitle, pageTitleEl;
let countyCol, schoolCol, termCol, examTypeCol, versionCol, daanCol, yearCol;
let studyarkOnlyEls;  // 【2026-07-24 新】所有 .studyark-only 元素 (text + filters)

function initDomRefs() {
    form = document.querySelector('form[action="/ui/search"]');
    if (!form) return;

    gradeSelect = $id("gradeSelect");
    subjectSelect = form.querySelector('select[name="subject"]');
    countySelect = $id("countySelect");
    schoolNameSelect = $id("schoolNameSelect");
    submitBtn = form.querySelector('button[type="submit"]');
    cardTitle = document.querySelector('h5.card-title');
    pageTitleEl = document.querySelector('h1.page-title');

    function findCol(name) {
        const el = form.querySelector(`[name="${name}"]`);
        if (!el) return null;
        return el.closest('.col-md-2, .col-md-3, .col-md-4, .col-md-6');
    }

    countyCol = findCol("county");
    schoolCol = schoolNameSelect ? schoolNameSelect.closest('.col-md-2, .col-md-3, .col-md-4, .col-md-6') : null;
    yearCol = findCol("school_year");
    termCol = findCol("school_term");
    examTypeCol = findCol("exam_type");
    versionCol = findCol("version");
    daanCol = findCol("daan");

    // 【2026-07-24 新】所有 StudyArk-only 元素 (text + 限流警語 + 各 filter col)
    studyarkOnlyEls = $all('.studyark-only');
    // 【2026-08-17 新】讓 dropdown 變數在 initDomRefs 裡 assign
    examTypeSelect = $id("examTypeSelect");
    subjectSelectEl = $id("subjectSelect");
    versionSelectEl = $id("versionSelect");
    schoolTermSelectEl = $id("schoolTermSelect");
    // 【2026-08-17 改】學年從 input+datalist 改成 select dropdown
    yearSelectEl = $id("schoolYearSelect");
    daanSelectEl = $id("daanSelect");

    // 【2026-08-17 改】預設載入時填 dropdown (避免顯示「只有不限」)
    if (yearSelectEl && yearSelectEl.options.length <= 1) {
        const defaultYears = [];
        for (let y = 115; y >= 83; y--) defaultYears.push(String(y));
        populateYears(yearSelectEl, defaultYears);
    }
    if (subjectSelectEl && subjectSelectEl.options.length <= 1) {
        fillSelectOptions(subjectSelectEl,
            ["數學","國語","英語","生活","健康與體育","社會","地理","歷史","理化","公民","自然","作文"],
            subjectSelectEl.value);
    }
    if (examTypeSelect && examTypeSelect.options.length <= 1) {
        fillSelectOptions(examTypeSelect, ["期中考", "期末考"], examTypeSelect.value);
    }
    if (versionSelectEl && versionSelectEl.options.length <= 1) {
        fillSelectOptions(versionSelectEl, ["康軒","翰林","南一","佳音","何嘉仁","龍騰","泰宇","三民","其他"],
            versionSelectEl.value);
    }
    if (schoolTermSelectEl && schoolTermSelectEl.options.length <= 1) {
        fillSelectOptions(schoolTermSelectEl, ["上學期","下學期"], schoolTermSelectEl.value);
    }
}

// =========================================================
// Helpers
// =========================================================
function clearChildren(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
}

function addOption(select, value, text) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = text;
    select.appendChild(opt);
}

function populateSubjects(select, subjects, placeholder) {
    if (!select) return;
    clearChildren(select);
    addOption(select, "", placeholder || "不限");
    subjects.forEach(s => addOption(select, s, s));
}

function showEl(el) {
    if (el) el.style.display = "";
}
function hideEl(el) {
    if (el) el.style.display = "none";
}

// Disable all form controls inside an element (遞選) - 用來確保隱藏 filter 不會被 submit
function disableIn(el) {
    if (!el) return;
    el.querySelectorAll("input, select, textarea, button").forEach(c => {
        c.disabled = true;
    });
}
function enableIn(el) {
    if (!el) return;
    el.querySelectorAll("input, select, textarea, button").forEach(c => {
        c.disabled = false;
    });
}

// 【2026-07-24 新】StudyArk-only 元素切換
// 【2026-08-17 改】subject column 不在 studyark-only (CAP/CEEC 也用)
//   yearCol 已經在 setModeCap/Ceec 中 showEl + enableIn
function showStudyarkOnly() {
    studyarkOnlyEls.forEach(el => {
        el.style.display = "";
        enableIn(el);
    });
    if (subjectSelectEl) {
        subjectSelectEl.style.display = "";
        subjectSelectEl.disabled = false;
    }
}
function hideStudyarkOnly() {
    studyarkOnlyEls.forEach(el => {
        el.style.display = "none";
        disableIn(el);
    });
    // 保留 subject column 顯示 (CAP/CEEC 用)
    if (subjectSelectEl) {
        subjectSelectEl.style.display = "";
        subjectSelectEl.disabled = false;
    }
}

// =========================================================
// Mode switch
// =========================================================
function setModeStudyark() {
    currentMode = "studyark";
    populateSubjects(subjectSelect, getSubjects("studyark"), "不限");

    showStudyarkOnly();
    showEl(yearCol);       enableIn(yearCol);

    form.action = "/ui/search";
    submitBtn.textContent = "🔍 搜尋";
    submitBtn.className = "btn btn-primary mt-3";
    if (cardTitle) cardTitle.textContent = "搜尋考古題 (StudyArk)";
    if (pageTitleEl) pageTitleEl.textContent = "考題搜尋";
}

function setModeCap() {
    currentMode = "cap";
    populateSubjects(subjectSelect, getSubjects("cap"), "不限");
    populateYears(yearSelectEl, getYears("cap"));

    hideStudyarkOnly();  // 隱藏 StudyArk 文字 + 限流警語 + 所有 StudyArk filters
    showEl(yearCol);      enableIn(yearCol);  // 學年保留 (CAP filter by year)
    // 【2026-08-17 修】gradeSelect 永遠 enabled (grade 本身是 mode selector, 選會考才能送出)
    if (gradeSelect) gradeSelect.disabled = false;

    form.action = "/ui/search";
    submitBtn.textContent = "🔍 搜尋 會考考題";
    submitBtn.className = "btn btn-warning mt-3";
    if (cardTitle) cardTitle.textContent = "📙 搜尋歷屆會考 (CAP)";
    if (pageTitleEl) pageTitleEl.textContent = "📙 會考考題搜尋";
}

function setModeCeec() {
    currentMode = "ceec";
    populateSubjects(subjectSelect, getSubjects("ceec"), "不限");
    populateYears(yearSelectEl, getYears("ceec"));

    hideStudyarkOnly();  // 隱藏 StudyArk 文字 + 限流警語 + 所有 StudyArk filters
    showEl(yearCol);      enableIn(yearCol);  // 學年保留 (CEEC filter by year)
    // 【2026-08-17 修】gradeSelect 永遠 enabled (grade 本身是 mode selector, 選大考才能送出)
    if (gradeSelect) gradeSelect.disabled = false;

    form.action = "/ui/search";
    submitBtn.textContent = "🔍 搜尋 大考考題";
    submitBtn.className = "btn btn-warning mt-3";
    if (cardTitle) cardTitle.textContent = "📕 搜尋歷屆大學入學考 (CEEC)";
    if (pageTitleEl) pageTitleEl.textContent = "📕 大考考題搜尋";
}

// =========================================================
// Main handler
// =========================================================
function onGradeChange() {
    const v = gradeSelect.value;
    if (v === "會考") {
        setModeCap();
    } else if (v === "大學入學考") {
        setModeCeec();
    } else {
        setModeStudyark();
    }
}

// =========================================================
// Init
// =========================================================
// =========================================================
// 【2026-07-31 新】County → Schools dependent dropdown
// 從 disk 自動列出有資料的學校 (避免搜尋沒有的學校)
// =========================================================
async function fetchAvailableSchools(county) {
    if (!county) {
        clearChildren(schoolNameSelect);
        addOption(schoolNameSelect, "", "請先選縣市");
        schoolNameSelect.disabled = true;
        const hint = $id("schoolCountHint");
        if (hint) hint.textContent = "";
        return;
    }
    try {
        const resp = await fetch(`/api/available-schools?county=${encodeURIComponent(county)}`);
        if (!resp.ok) throw new Error("API 失敗");
        const data = await resp.json();
        const schools = (data[county] || []);
        clearChildren(schoolNameSelect);
        addOption(schoolNameSelect, "", `不限 (${schools.length} 校)`);
        schools.forEach(s => {
            // s.name 是 "高雄市七賢國中", 顯示為 "高雄市七賢國中 (123 檔)"
            addOption(schoolNameSelect, s.name, `${s.name} (${s.file_count} 檔)`);
        });
        schoolNameSelect.disabled = false;
        const hint = $id("schoolCountHint");
        if (hint) hint.textContent = `📁 ${schools.length} 校, ${schools.reduce((a, s) => a + s.file_count, 0)} 檔`;
    } catch (err) {
        console.error("fetchAvailableSchools:", err);
        clearChildren(schoolNameSelect);
        addOption(schoolNameSelect, "", "載入失敗,請重試");
        schoolNameSelect.disabled = true;
    }
}

function onCountyChange() {
    if (!countySelect) return;
    const county = countySelect.value;
    // 縣市 id → 中文名 (matching backend tw_counties.py)
    const id_to_name = {
        "taipei": "臺北市",
        "new_taipei": "新北市",
        "keelung": "基隆市",
        "yilan": "宜蘭縣",
        "taoyuan": "桃園市",
        "hsinchu_city": "新竹市",
        "hsinchu_county": "新竹縣",
        "miaoli": "苗栗縣",
        "taichung": "臺中市",
        "changhua": "彰化縣",
        "nantou": "南投縣",
        "yunlin": "雲林縣",
        "chiayi_city": "嘉義市",
        "chiayi_county": "嘉義縣",
        "tainan": "臺南市",
        "kaohsiung": "高雄市",
        "pingtung": "屏東縣",
        "taitung": "臺東縣",
        "hualien": "花蓮縣",
        "penghu": "澎湖縣",
        "kinmen": "金門縣",
        "lienchiang": "連江縣",
    };
    const countyName = id_to_name[county] || county;
    fetchAvailableSchools(countyName);
}

document.addEventListener("DOMContentLoaded", () => {
    initDomRefs();
    if (!form || !gradeSelect) return;
    gradeSelect.addEventListener("change", onGradeChange);
    countySelect.addEventListener("change", onCountyChange);

    // 如果頁面載入時 grade 已經是會考/大考 (例如 user bookmarked), 觸發一次
    if (gradeSelect.value === "會考") {
        setModeCap();
    } else if (gradeSelect.value === "大學入學考") {
        setModeCeec();
    }
});

let yearSelectEl;  // 【2026-08-17 改】學年 select (initDomRefs 裡 assign)

// ============================================================
// 【2026-08-17 新】Cascading dropdown: 點 county → 學校; 點學校 → 其他 dropdowns
// ============================================================
// 在 initDomRefs 裡 init (避免這邊就跑 DOM query)

async function fetchAvailableFilters(county, schoolName) {
    // 學校為空時只 filter county
    const params = new URLSearchParams();
    if (county) params.set("county", county);
    if (schoolName) params.set("school_name", schoolName);
    const url = "/api/available-filters?" + params.toString();
    try {
        const r = await fetch(url);
        if (!r.ok) return null;
        return await r.json();
    } catch (e) {
        console.warn("fetchAvailableFilters failed:", e);
        return null;
    }
}

function fillSelectOptions(selectEl, options, currentValue) {
    if (!selectEl) return;
    // 保留第一個 option (不限)
    const defaultOpt = selectEl.options[0];
    selectEl.innerHTML = "";
    if (defaultOpt) selectEl.appendChild(defaultOpt);
    // 重建其他 options
    options.forEach(v => {
        const o = document.createElement("option");
        o.value = v;
        o.textContent = v;
        selectEl.appendChild(o);
    });
    // 還原 current value (如果還在清單裡)
    if (currentValue && Array.from(selectEl.options).some(o => o.value === currentValue)) {
        selectEl.value = currentValue;
    } else {
        selectEl.value = "";  // 不在清單 → reset
    }
}

function fillDatalistOptions(datalistEl, options) {
    if (!datalistEl) return;
    datalistEl.innerHTML = "";
    options.forEach(v => {
        const o = document.createElement("option");
        o.value = v;
        datalistEl.appendChild(o);
    });
}

async function onSchoolChange() {
    if (!schoolNameSelect) return;
    const county = countySelect ? (countySelect.value || "") : "";
    const schoolName = schoolNameSelect.value || "";
    // 沒選學校 → 用 template 預設值 (避免顯示 700+ garbage subject 從 DriveFolder)
    if (!schoolName) {
        fillSelectOptions(examTypeSelect, ["期中考", "期末考"], examTypeSelect ? examTypeSelect.value : "");
        fillSelectOptions(subjectSelectEl,
            ["數學","國語","英語","生活","健康與體育","社會","地理","歷史","理化","公民","自然","作文"],
            subjectSelectEl ? subjectSelectEl.value : "");
        fillSelectOptions(versionSelectEl, ["康軒","翰林","南一","佳音","何嘉仁","龍騰","泰宇","三民","其他"],
            versionSelectEl ? versionSelectEl.value : "");
        fillSelectOptions(schoolTermSelectEl, ["上學期","下學期"], schoolTermSelectEl ? schoolTermSelectEl.value : "");
        // 學年 default: 83-115
        const defaultYears = [];
        for (let y = 115; y >= 83; y--) defaultYears.push(String(y));
        populateYears(yearSelectEl, defaultYears);
        return;
    }
    // 有選學校 → 從 DB 拿該學校有的 filter values (避免 0 hit)
    const filters = await fetchAvailableFilters(county, schoolName);
    if (!filters) return;
    fillSelectOptions(examTypeSelect, filters.exam_type || [], examTypeSelect ? examTypeSelect.value : "");
    fillSelectOptions(subjectSelectEl, filters.subject || [], subjectSelectEl ? subjectSelectEl.value : "");
    fillSelectOptions(versionSelectEl, filters.version || [], versionSelectEl ? versionSelectEl.value : "");
    fillSelectOptions(schoolTermSelectEl, filters.school_term || [], schoolTermSelectEl ? schoolTermSelectEl.value : "");
    populateYears(yearSelectEl, filters.school_year || []);
    // 【2026-08-17 新】Grade cascading: 只列該學校有的年級. 統一考試永遠保留.
    updateGradeOptions(filters.grade || []);
}

// Map DB 的 grade 到 optgroup category (elementary / junior / senior / unset)
function gradeToCategory(g) {
    const elementary = ["一年級","二年級","三年級","四年級","五年級","六年級"];
    const junior = ["七年級","八年級","九年級"];
    const senior = ["高一","高二","高三"];
    if (elementary.includes(g)) return "elementary";
    if (junior.includes(g)) return "junior";
    if (senior.includes(g)) return "senior";
    return null;  // unknown
}

function updateGradeOptions(schoolGrades) {
    const gradeSel = document.getElementById("gradeSelect");
    if (!gradeSel) return;
    // 計算 school 涵蓋的 category
    const categories = new Set();
    schoolGrades.forEach(g => {
        const c = gradeToCategory(g);
        if (c) categories.add(c);
    });
    // 預設: 三類全顯示 (沒選學校時)
    const showAll = categories.size === 0;
    const groups = {
        elementary: document.getElementById("gradeOptgroupElementary"),
        junior: document.getElementById("gradeOptgroupJunior"),
        senior: document.getElementById("gradeOptgroupSenior"),
    };
    for (const [cat, group] of Object.entries(groups)) {
        if (!group) continue;
        group.disabled = !showAll && !categories.has(cat);
        // disabled optgroup 在瀏覽器中還是可見, 但選項不可選
        group.hidden = !showAll && !categories.has(cat);  // 完全隱藏
    }
}

// 在 onCountyChange 末尾 (load 學校後), 如果有選學校 → 也 call fetchAvailableFilters
const _orig_fetchAvailableSchools = fetchAvailableSchools;
// (Note: 實際 onCountyChange 用的是 fetchAvailableSchools 函式定義, 不容易 hook)
// 直接在 onCountyChange 結束時多觸發一次 fetchAvailableFilters

// 在 DOMContentLoaded 加 event listener
document.addEventListener("DOMContentLoaded", () => {
    if (schoolNameSelect) {
        schoolNameSelect.addEventListener("change", onSchoolChange);
    }
    if (countySelect) {
        countySelect.addEventListener("change", () => {
            // 等 fetchAvailableSchools 完成後觸發 filter refresh
            setTimeout(() => onSchoolChange(), 100);
        });
    }
});
