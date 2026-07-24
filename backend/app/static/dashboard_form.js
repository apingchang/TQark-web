// =========================================================
// dashboard form 互動邏輯 (2026-07-24 新)
// 監聽 grade dropdown 變化, 動態切換科目清單 + 隱藏無關 filter
//
// 規則:
//   - 選擇「歷屆會考」 (value="會考")
//     → 顯示 CAP 科目清單
//     → 隱藏 縣市 / 學校 / 學期 / 段考 / 版本 / 含答案 filter
//     → 學年 保留 (讓 user 篩選特定年度)
//     → form action 改成 /ui/cap-exam, 搜尋按鈕變成「📙 前往 歷屆會考」
//
//   - 選擇「歷屆大學入學考」 (value="大學入學考")
//     → 顯示 CEEC 科目清單
//     → 隱藏 縣市 / 學校 / 學期 / 段考 / 版本 / 含答案 filter
//     → 學年 保留
//     → form action 改成 /ui/ceec-exam, 按鈕變成「📕 前往 歷屆大學入學考」
//
//   - 選擇一般年級 (一年級 ~ 高三)
//     → 顯示 StudyArk 科目清單
//     → 顯示所有 filter
//     → form action = /ui/search, 按鈕 = 「🔍 搜尋」
// =========================================================

const CAP_SUBJECTS = [
    "國文", "英語", "數學", "社會", "自然",
];

const CEEC_SUBJECTS = [
    "國文", "國綜", "國寫", "英文",
    "數學", "數甲", "數乙", "數a", "數b",
    "社會", "自然",
    "物理", "化學", "生物", "歷史", "地理", "公民",
];

const STUDYARK_SUBJECTS = [
    "數學", "國語", "英語", "生活", "健康與體育",
    "社會", "地理", "歷史", "理化", "公民", "自然", "作文",
];

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

let form, gradeSelect, subjectSelect, countySelect, schoolNameInput, submitBtn;
let countyCol, schoolCol, termCol, examTypeCol, versionCol, daanCol, yearCol;

function initDomRefs() {
    form = document.querySelector('form[action="/ui/search"]');
    if (!form) return;

    gradeSelect = $id("gradeSelect");
    subjectSelect = form.querySelector('select[name="subject"]');
    countySelect = $id("countySelect");
    schoolNameInput = $id("schoolNameInput");
    submitBtn = form.querySelector('button[type="submit"]');

    // 找各 filter 的 col-md-* container (向上找 col-md-X parent)
    function findCol(name) {
        const el = form.querySelector(`[name="${name}"]`);
        if (!el) return null;
        // 找最近的 col-md-* parent
        let p = el.closest('.col-md-2, .col-md-3, .col-md-4, .col-md-6');
        return p;
    }

    countyCol = findCol("county");
    schoolCol = schoolNameInput ? schoolNameInput.closest('.col-md-2, .col-md-3, .col-md-4, .col-md-6') : null;
    yearCol = findCol("school_year");
    termCol = findCol("school_term");
    examTypeCol = findCol("exam_type");
    versionCol = findCol("version");
    daanCol = findCol("daan");
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

// =========================================================
// Mode switch
// =========================================================
function setModeStudyark() {
    currentMode = "studyark";
    populateSubjects(subjectSelect, STUDYARK_SUBJECTS, "不限");

    showEl(countyCol);     enableIn(countyCol);
    showEl(schoolCol);     enableIn(schoolCol);
    showEl(termCol);       enableIn(termCol);
    showEl(examTypeCol);   enableIn(examTypeCol);
    showEl(versionCol);    enableIn(versionCol);
    showEl(daanCol);       enableIn(daanCol);
    showEl(yearCol);       enableIn(yearCol);

    form.action = "/ui/search";
    submitBtn.textContent = "🔍 搜尋";
    submitBtn.className = "btn btn-primary mt-3";
}

function setModeCap() {
    currentMode = "cap";
    populateSubjects(subjectSelect, CAP_SUBJECTS, "不限");

    hideEl(countyCol);     disableIn(countyCol);
    hideEl(schoolCol);     disableIn(schoolCol);
    hideEl(termCol);       disableIn(termCol);
    hideEl(examTypeCol);   disableIn(examTypeCol);
    hideEl(versionCol);    disableIn(versionCol);
    hideEl(daanCol);       disableIn(daanCol);
    showEl(yearCol);       enableIn(yearCol);  // 學年保留 (CAP filter by year)

    // 【2026-07-24 改】全部 form 都 submit 到 /ui/search
    //   後端根據 grade=會考 自動 render cap_exam.html 結果 (filter by subject + year)
    form.action = "/ui/search";
    submitBtn.textContent = "🔍 搜尋 歷屆會考";
    submitBtn.className = "btn btn-warning mt-3";
}

function setModeCeec() {
    currentMode = "ceec";
    populateSubjects(subjectSelect, CEEC_SUBJECTS, "不限");

    hideEl(countyCol);     disableIn(countyCol);
    hideEl(schoolCol);     disableIn(schoolCol);
    hideEl(termCol);       disableIn(termCol);
    hideEl(examTypeCol);   disableIn(examTypeCol);
    hideEl(versionCol);    disableIn(versionCol);
    hideEl(daanCol);       disableIn(daanCol);
    showEl(yearCol);       enableIn(yearCol);  // 學年保留 (CEEC filter by year)

    // 【2026-07-24 改】全部 form 都 submit 到 /ui/search
    //   後端根據 grade=大學入學考 自動 render ceec_exam.html 結果
    form.action = "/ui/search";
    submitBtn.textContent = "🔍 搜尋 歷屆大學入學考";
    submitBtn.className = "btn btn-warning mt-3";
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
document.addEventListener("DOMContentLoaded", () => {
    initDomRefs();
    if (!form || !gradeSelect) return;
    gradeSelect.addEventListener("change", onGradeChange);

    // 如果頁面載入時 grade 已經是會考/大考 (例如 user bookmarked), 觸發一次
    if (gradeSelect.value === "會考") {
        setModeCap();
    } else if (gradeSelect.value === "大學入學考") {
        setModeCeec();
    }
});