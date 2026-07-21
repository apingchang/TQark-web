"""
PDF title extractor - 抓 PDF 標題 → county + school name

Pipeline:
1. pdftotext (text-based PDFs, ~0.5s)
2. OCR: pdftoppm + tesseract chi_tra+eng (image-based PDFs, ~6s)
3. 都失敗 → 未註明
"""
import re
import subprocess
import shutil
import tempfile
from pathlib import Path

from app.scraper.school_stats import (
    COUNTY_PATTERNS,
    COUNTY_ALIASES,
)


def extract_pdf_title(pdf_path: Path, max_chars: int = 500) -> str | None:
    """從 PDF 抓前 max_chars 字 (用 pdftotext)"""
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return None
    
    try:
        result = subprocess.run(
            [pdftotext, str(pdf_path), "-"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout[:max_chars]
    except (subprocess.TimeoutExpired, Exception):
        return None


def extract_pdf_title_ocr(pdf_path: Path, max_chars: int = 500) -> str | None:
    """OCR fallback: PyMuPDF (auto-rotates) + tesseract"""
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return None
    
    try:
        import fitz  # PyMuPDF
    except ImportError:
        # Fallback to pdftoppm if PyMuPDF not available
        return _extract_pdf_title_ocr_pdftoppm(pdf_path, max_chars)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        try:
            doc = fitz.open(str(pdf_path))
            if len(doc) == 0:
                doc.close()
                return None
            page = doc[0]
            pix = page.get_pixmap(dpi=300)
            png_path = tmp / "page-1.png"
            pix.save(str(png_path))
            doc.close()
            
            result = subprocess.run(
                [tesseract, str(png_path), "-", "-l", "chi_tra", "--psm", "6"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                return None
            return result.stdout[:max_chars]
        except (subprocess.TimeoutExpired, Exception):
            return None


def _extract_pdf_title_ocr_pdftoppm(pdf_path: Path, max_chars: int = 500) -> str | None:
    """Legacy OCR: pdftoppm (no rotation handling)"""
    pdftoppm = shutil.which("pdftoppm")
    tesseract = shutil.which("tesseract")
    if not (pdftoppm and tesseract):
        return None
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        png_prefix = tmp / "page"
        try:
            subprocess.run(
                [pdftoppm, "-r", "400", "-f", "1", "-l", "1", "-png", str(pdf_path), str(png_prefix)],
                capture_output=True,
                timeout=20,
                check=False,
            )
            png_path = tmp / "page-1.png"
            if not png_path.exists():
                return None
            
            result = subprocess.run(
                [tesseract, str(png_path), "-", "-l", "chi_tra+eng", "--psm", "1"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if result.returncode != 0:
                return None
            return result.stdout[:max_chars]
        except (subprocess.TimeoutExpired, Exception):
            return None


def _parse_school_title(text: str) -> dict:
    """從 text parse 出 county + school_name (內部 helper)
    
    對 OCR 結果掃描前 10 行, 因為第一行常是噪聲。
    County = 「未註明」 → 「其他縣市」(per William request, 保留作為 unknown category)
    """
    if not text:
        return {"county": "其他縣市", "school_name": "未註明", "school_short": "未註明", "title": None}
    
    lines = text.split("\n")[:10]  # 前 10 行
    first_line = lines[0].strip() if lines else ""
    cleaned_full = re.sub(r"\s+", "", "\n".join(lines))
    
    county = "其他縣市"
    school_name = "未註明"
    
    for c in COUNTY_PATTERNS:
        if cleaned_full.startswith(c) or c in cleaned_full[:100]:
            county = COUNTY_ALIASES.get(c, c)
            # 找 county 在 cleaned_full 的位置
            idx = cleaned_full.find(c)
            after_county = cleaned_full[idx + len(c):][:50]
            m = re.match(
                r"((?:市立|縣立|私立|國立|鄉立|鎮立)?[^0-9]+?(?:"
                r"國民中學|國民小學|國中|國小|高中|高商|高工|高職|"
                r"完全中學|附屬中學|附設國中|附設國小|實驗國中|實驗國小|"
                r"高級中學|高級職業|高級商業|高級工業))",
                after_county,
            )
            if m:
                # 用 normalized county + 學校名 (避免舊名)
                school_name = county + m.group(1)
            else:
                school_name = county + after_county[:12]
            break
    
    school_short = school_name[len(county):] if county != "其他縣市" else school_name
    
    return {
        "county": county,
        "school_name": school_name,
        "school_short": school_short,
        "title": first_line,
    }


def extract_school_from_pdf(pdf_path: Path) -> dict:
    """從 PDF parse county + school_name
    
    Pipeline:
    1. pdftotext (text-based, ~0.5s)
    2. OCR fallback (image-based, ~6s)
    3. 都失敗 → 未註明
    
    邏輯: pdftotext 只有在真的有學校名 (有 county match) 才 return, 否則一律 OCR。
    """
    # Try 1: pdftotext
    text = extract_pdf_title(pdf_path)
    if text:
        result = _parse_school_title(text)
        if result["county"] != "其他縣市":
            # 找到 county → 認可這個結果
            return result
    
    # Try 2: OCR fallback (image-based PDF)
    ocr_text = extract_pdf_title_ocr(pdf_path)
    if ocr_text:
        result = _parse_school_title(ocr_text)
        result["title"] = f"[OCR] {result['title'] or ''}"
        return result
    
    return {"county": "其他縣市", "school_name": "未註明", "school_short": "未註明", "title": None}


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        pdf_path = Path(sys.argv[1])
    else:
        pdf_path = next(Path("/mnt/my_book/考題收集").rglob("*.pdf"))
    
    if not pdf_path.exists():
        print(f"Not found: {pdf_path}")
        sys.exit(1)
    
    info = extract_school_from_pdf(pdf_path)
    print(f"PDF: {pdf_path}")
    print(f"  Title:    {info['title']}")
    print(f"  County:   {info['county']}")
    print(f"  School:   {info['school_name']}")
    print(f"  Short:    {info['school_short']}")