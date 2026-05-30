"""
TXT → EPUB 변환기 v1.2.2

추가/수정 내역:
- 줄 위치 계산 오류 수정: splitlines(True)로 원본 줄바꿈 길이 보존
- EPUB 본문에서 챕터 제목 줄 중복 제거
- 챕터 선택 삭제 기능
- 챕터 클릭 시 본문 미리보기
- 정규식 직접 입력/적용 기능
- 줄 번호 기준 챕터 수동 추가 기능
- 메타데이터/판권정보 입력칸 확장
- 권 분할 초안 생성 및 권별 EPUB 생성
- 챕터 변경 시 권 분할표 자동 무효화
- 권별 EPUB 생성 시 동일 ISBN 중복 사용 방지

필요 패키지:
    pip install PySide6 ebooklib charset-normalizer hanja

실행:
    python txt_to_epub_converter_v1_2_2.py
"""

import os
import re
import html
import json
import uuid
import urllib.parse
import subprocess
import base64
import webbrowser
import sys
from datetime import date
from dataclasses import dataclass
from typing import List, Optional, Tuple

sys.dont_write_bytecode = True

from charset_normalizer import from_path
from ebooklib import epub
import hanja

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFileDialog, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QTextEdit, QTableWidget, QTableWidgetItem,
    QMessageBox, QSplitter, QFormLayout, QTabWidget, QCheckBox,
    QStackedWidget, QSlider, QComboBox, QHeaderView,
)
from PySide6.QtCore import Qt, QByteArray, QBuffer, QIODevice, QThread, Signal
from PySide6.QtGui import QImage, QTextCursor, QIntValidator, QKeySequence, QShortcut


class Epub2Writer(epub.EpubWriter):
    def _write_opf_metadata(self, root):
        nsmap = {"dc": epub.NAMESPACES["DC"], "opf": epub.NAMESPACES["OPF"]}
        nsmap.update(self.book.namespaces)
        metadata = epub.etree.SubElement(root, "metadata", nsmap=nsmap)

        for ns_name, values in epub.six.iteritems(self.book.metadata):
            if ns_name == epub.NAMESPACES["OPF"]:
                for values2 in values.values():
                    for text, attrs in values2:
                        attrs = attrs or {}
                        if attrs.get("property") == "dcterms:modified":
                            continue
                        try:
                            el = epub.etree.SubElement(metadata, "meta", attrs)
                            if text:
                                el.text = text
                        except ValueError:
                            pass
            else:
                for name, values2 in epub.six.iteritems(values):
                    for text, attrs in values2:
                        try:
                            attrs = attrs or {}
                            if ns_name:
                                el = epub.etree.SubElement(metadata, f"{{{ns_name}}}{name}", attrs)
                            else:
                                el = epub.etree.SubElement(metadata, name, attrs)
                            el.text = text
                        except ValueError:
                            pass

    def _write_opf_manifest(self, root):
        manifest = epub.etree.SubElement(root, "manifest")
        ncx_id = None

        for item in self.book.get_items():
            if not item.manifest:
                continue
            if isinstance(item, epub.EpubNav):
                continue
            if isinstance(item, epub.EpubNcx):
                ncx_id = item.id
                epub.etree.SubElement(
                    manifest,
                    "item",
                    {"href": item.file_name, "id": item.id, "media-type": item.media_type},
                )
                continue

            epub.etree.SubElement(
                manifest,
                "item",
                {"href": item.file_name, "id": item.id, "media-type": item.media_type},
            )

        return ncx_id

    def _write_opf(self):
        root = epub.etree.Element(
            "package",
            {
                "xmlns": epub.NAMESPACES["OPF"],
                "unique-identifier": self.book.IDENTIFIER_ID,
                "version": "2.0",
            },
        )

        self._write_opf_metadata(root)
        ncx_id = self._write_opf_manifest(root)
        self._write_opf_spine(root, ncx_id)
        self._write_opf_guide(root)
        self._write_opf_bindings(root)
        self._write_opf_file(root)

    def _write_items(self):
        for item in self.book.get_items():
            if isinstance(item, epub.EpubNav):
                continue
            if isinstance(item, epub.EpubNcx):
                self.out.writestr(
                    f"{self.book.FOLDER_NAME}/{item.file_name}",
                    self._get_ncx(),
                )
            elif isinstance(item, epub.EpubHtml) and item.content:
                content = item.content.encode("utf-8") if isinstance(item.content, str) else item.content
                self.out.writestr(
                    f"{self.book.FOLDER_NAME}/{item.file_name}",
                    content,
                )
            elif item.manifest:
                self.out.writestr(
                    f"{self.book.FOLDER_NAME}/{item.file_name}",
                    item.get_content(),
                )
            else:
                self.out.writestr(item.file_name, item.get_content())


def write_epub2(path: str, book: epub.EpubBook, options: Optional[dict] = None):
    writer = Epub2Writer(path, book, options)
    writer.process()
    writer.write()
    return True


@dataclass
class Chapter:
    number: Optional[int]
    title: str
    line_index: int
    start_char: int
    end_char: int = 0
    marker: str = ""
    subtitle: str = ""
    subtitle_line_index: Optional[int] = None
    subtitle_prefix: str = ""
    manual_body_start: bool = False
    manual_delete_lines: int = 0
    edited_body: Optional[str] = None
    edited_body_delete_lines: int = 0


@dataclass
class VolumeRange:
    volume: int
    start_index: int
    end_index: int


@dataclass
class SampleRuleGuess:
    marker: str
    subtitle: str
    subtitle_from_next_line: bool


@dataclass
class IsbnRecord:
    title: str
    author: str = ""
    publisher: str = ""
    isbn: str = ""
    date: str = ""
    binding: str = ""
    price: str = ""
    keywords: str = ""
    detail_isbn: str = ""
    cip_id: str = ""


@dataclass
class IsbnSearchResult:
    records: List[IsbnRecord]
    current_page: int = 1
    total_count: int = 0
    page_unit: int = 10

    @property
    def total_pages(self) -> int:
        if self.total_count <= 0:
            return 1 if self.records else 0
        return max(1, (self.total_count + self.page_unit - 1) // self.page_unit)


@dataclass
class ValidationIssue:
    kind: str
    number: Optional[int]
    chapter_index: int
    message: str


DEFAULT_CHAPTER_PATTERNS = [
    r"^\s*(?:제\s*)?(\d{1,5})\s*화\b.*$",
    r"^\s*(?:제\s*)?(\d{1,5})\s*장\b.*$",
    r"^\s*(?:#\s*)?(\d{1,5})\s*[\.\)]\s*.*$",
    r"^\s*#\s*(\d{1,5})\b.*$",
    r"^\s*episode\s*(\d{1,5})\b.*$",
    r"^\s*chapter\s*(\d{1,5})\b.*$",
    r"^\s*(프롤로그|prologue)\s*$",
    r"^\s*(에필로그|epilogue)\s*$",
    r"^\s*(외전)\b.*$",
]


class TxtEpubConverter:
    def __init__(self):
        self.txt_path: Optional[str] = None
        self.cover_path: Optional[str] = None
        self.font_path: Optional[str] = None
        self.text: str = ""
        self.lines: List[str] = []
        self.line_offsets: List[int] = []
        self.chapters: List[Chapter] = []
        self.current_regex: Optional[str] = None
        self.volume_ranges: List[VolumeRange] = []
        self.rejected_chapter_titles: List[str] = []
        self.ignored_body_line_indexes: set[int] = set()

    def load_txt(self, path: str) -> str:
        result = from_path(path).best()
        if result is None:
            raise ValueError("인코딩을 감지하지 못했습니다.")

        encoding = result.encoding or "utf-8"
        with open(path, "r", encoding=encoding, errors="replace") as f:
            self.text = f.read()
        self.text = self.text.lstrip("\ufeff")

        self.txt_path = path
        raw_lines = self.text.splitlines(True)
        self.lines = [line.rstrip("\r\n") for line in raw_lines]
        self.line_offsets = []

        pos = 0
        for raw_line in raw_lines:
            self.line_offsets.append(pos)
            pos += len(raw_line)

        self.chapters = []
        self.volume_ranges = []
        self.rejected_chapter_titles = []
        self.ignored_body_line_indexes = set()
        return encoding

    def detect_chapters_default(self) -> List[Chapter]:
        chapters: List[Chapter] = []
        compiled = [re.compile(p, re.IGNORECASE) for p in DEFAULT_CHAPTER_PATTERNS]

        for i, line in enumerate(self.lines):
            stripped = line.strip()
            if not stripped or len(stripped) > 80:
                continue

            for pattern in compiled:
                match = pattern.match(stripped)
                if match:
                    number = self._extract_number_from_match(match)
                    marker = self._episode_marker_from_number(number) or self._normalize_marker_for_title(stripped)
                    title = marker or stripped
                    chapters.append(Chapter(number, title, i, self.line_offsets[i], marker=marker or stripped))
                    break

        self.chapters = self._finalize_chapter_ranges(chapters)
        self.volume_ranges = []
        self.current_regex = None
        return self.chapters

    def build_regex_from_samples(self, sample_text: str) -> str:
        samples = [line.strip() for line in sample_text.splitlines() if line.strip()]
        if not samples:
            raise ValueError("샘플 챕터 제목을 입력하세요.")

        units = []
        for sample in samples:
            m = re.search(r"(\d{1,5})\s*([화장회])", sample)
            if m:
                units.append(m.group(2))

        if units:
            unit = max(set(units), key=units.count)
            return rf"^\s*(?:제\s*)?(\d{{1,5}})\s*{re.escape(unit)}\b.*$"

        if any(re.search(r"chapter\s*\d+", s, re.IGNORECASE) for s in samples):
            return r"^\s*chapter\s*(?P<number>\d{1,5})\b.*$"

        if any(re.search(r"episode\s*\d+", s, re.IGNORECASE) for s in samples):
            return r"^\s*episode\s*(?P<number>\d{1,5})\b.*$"

        if any(re.search(r"^\s*#\s*\d{1,5}\b", s) for s in samples):
            return r"^\s*#\s*(?P<number>\d{1,5})\b.*$"

        if any(re.search(r"^\s*\d{1,5}\s*[\.\)]", s) for s in samples):
            return r"^\s*(?P<number>\d{1,5})\s*[\.\)]\s*.*$"

        if all(re.fullmatch(r"\s*\d{1,5}\s*", s) for s in samples):
            return r"^\s*(?P<number>\d{1,5})\s*$"

        if all(re.search(r"\d{1,5}", s) for s in samples):
            return r"^\s*.*?(\d{1,5}).*$"

        raise ValueError("샘플에서 공통 챕터 번호 규칙을 찾지 못했습니다.")

    def find_chapter_sample_lines(self, limit: int = 5) -> List[str]:
        candidates = {}
        for number, line_index in self.find_sequential_chapter_number_lines(limit):
            if number not in candidates:
                candidates[number] = self._sample_with_context_lines(line_index)
                if len(candidates) >= limit:
                    break

        return [candidates[n] for n in sorted(candidates)]

    def find_sequential_chapter_number_lines(self, limit: int = 5, min_run: int = 3) -> List[tuple[int, int]]:
        pattern_rows: dict[str, List[tuple[int, int]]] = {}
        for idx, line in enumerate(self.lines):
            stripped = line.strip()
            if not stripped or len(stripped) > 120:
                continue
            token = self._chapter_number_token_from_line(stripped)
            if token is None:
                continue
            key, number = token
            if 0 <= number <= 99999:
                pattern_rows.setdefault(key, []).append((number, idx))

        best_run: List[tuple[int, int]] = []
        for row in pattern_rows.values():
            run = self._best_sequential_run(row)
            if self._looks_like_compact_numbered_list(run):
                continue
            if run and (not best_run or (run[0][0] in {0, 1} and best_run[0][0] not in {0, 1})):
                best_run = run
            elif run and best_run and run[0][0] not in {0, 1} and best_run[0][0] in {0, 1}:
                continue
            elif len(run) > len(best_run):
                best_run = run
            elif len(run) == len(best_run) and run and best_run and run[-1][1] < best_run[-1][1]:
                best_run = run

        if len(best_run) < min_run:
            return []
        return best_run[:limit]

    def _looks_like_compact_numbered_list(self, run: List[tuple[int, int]]) -> bool:
        if len(self.lines) < 1000 or len(run) < 3 or len(run) > 10:
            return False
        line_span = run[-1][1] - run[0][1]
        return line_span <= len(run) * 3

    def find_sequential_number_only_lines(self, limit: int = 5, min_run: int = 3) -> List[tuple[int, int]]:
        number_lines: List[tuple[int, int]] = []
        pattern = re.compile(r"^\s*(\d{1,5})\s*$")
        for idx, line in enumerate(self.lines):
            match = pattern.match(line)
            if not match:
                continue
            number = int(match.group(1))
            if 1 <= number <= max(limit, min_run) + 20:
                number_lines.append((number, idx))

        best_run = self._best_sequential_run(number_lines)
        if len(best_run) < min_run:
            return []
        return best_run[:limit]

    def _best_sequential_run(self, number_lines: List[tuple[int, int]]) -> List[tuple[int, int]]:
        best_run: List[tuple[int, int]] = []
        best_from_one: List[tuple[int, int]] = []
        best_from_zero: List[tuple[int, int]] = []
        current: List[tuple[int, int]] = []
        for number, idx in number_lines:
            if not current:
                current = [(number, idx)]
            elif number == current[-1][0] + 1:
                current.append((number, idx))
            elif number in {0, 1}:
                current = [(number, idx)]
            else:
                current = [(number, idx)]
            if len(current) > len(best_run):
                best_run = list(current)
            if current and current[0][0] == 0 and len(current) > len(best_from_zero):
                best_from_zero = list(current)
            if current and current[0][0] == 1 and len(current) > len(best_from_one):
                best_from_one = list(current)
        return best_from_zero or best_from_one or best_run

    def _chapter_number_token_from_line(self, line: str) -> Optional[tuple[str, int]]:
        line = line.lstrip("\ufeff")
        patterns = [
            ("unit", r"^\s*(?:제\s*)?(\d{1,5})\s*(화|장|회|차)(?=\s|[\.\)\]]|$)"),
            ("chapter", r"^\s*(chapter)\s*(\d{1,5})\b"),
            ("episode", r"^\s*(episode)\s*(\d{1,5})\b"),
            ("hash", r"^\s*(#)\s*(\d{1,5})\b"),
            ("dot", r"^\s*(\d{1,5})\s*([\.\)])"),
            ("plain", r"^\s*(\d{1,5})\s*$"),
        ]
        for key, pattern in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                number = self._extract_number_from_match(match)
                if number is None:
                    continue
                shape = re.sub(r"\d{1,5}", "{n}", match.group(0).strip(), count=1)
                shape = re.sub(r"\s+", " ", shape).lower()
                return f"{key}:{shape}", number
        return None

    def _chapter_marker_match_regex(self) -> str:
        return (
            r"(?:제\s*)?\d{1,5}\s*(?:화|장|회|차)(?=\s|[\.\)\]]|$)\s*[\.\)\]]?"
            r"|chapter\s*\d{1,5}\b"
            r"|episode\s*\d{1,5}\b"
            r"|#\s*\d{1,5}\b"
            r"|^\s*\d{1,5}\s*[\.\)]"
            r"|^\s*\d{1,5}\s*$"
        )

    def find_extra_chapter_sample_lines(self, regular_regex: str, limit: int = 5, max_title_len: int = 80) -> List[str]:
        if not regular_regex.strip():
            raise ValueError("먼저 정규 챕터 분류기호를 지정하세요.")
        regular_chapters = self._finalize_chapter_ranges(
            self._detect_chapter_candidates(regular_regex, max_title_len)
        )
        if not regular_chapters:
            raise ValueError("정규 챕터 후보를 먼저 찾지 못했습니다.")

        last_regular = regular_chapters[-1]
        broad = re.compile(
            r"^\s*(?:제\s*)?\d{1,5}\s*(?:화|장|회|차)\b.*$"
            r"|^\s*chapter\s*\d{1,5}\b.*$"
            r"|^\s*episode\s*\d{1,5}\b.*$"
            r"|^\s*#\s*\d{1,5}\b.*$"
            r"|^\s*\d{1,5}\s*[\.\)]\s*.*$"
            r"|^\s*.{0,40}\d{1,5}\s*(?:화|장|회|차)\s*(?:외전|후기|후일담|번외|특별편)\b.*$",
            re.IGNORECASE,
        )
        samples = []
        for i, line in enumerate(self.lines):
            if self.line_offsets[i] <= last_regular.start_char:
                continue
            stripped = line.strip()
            if not stripped or len(stripped) > max_title_len:
                continue
            if broad.match(stripped):
                samples.append(self._sample_with_context_lines(i))
                if len(samples) >= limit:
                    break
        return samples

    def guess_rule_from_sample_lines(
        self, samples: List[str], ignore_prev_line: bool = False
    ) -> Optional[SampleRuleGuess]:
        parsed = []
        for sample in samples:
            lines = [line.strip() for line in sample.splitlines() if line.strip()]
            if ignore_prev_line and len(lines) >= 2:
                lines = lines[1:]
            if not lines:
                continue
            marker_line_index, marker_match = self._sample_marker_match(lines)
            if marker_line_index is None or marker_match is None:
                continue
            prev_line = lines[marker_line_index - 1] if marker_line_index > 0 else ""
            next_line = lines[marker_line_index + 1] if marker_line_index + 1 < len(lines) else ""
            marker_line = lines[marker_line_index]
            match = marker_match
            if match:
                parsed.append((marker_line, prev_line, next_line, match))

        if not parsed:
            return None

        first_line, prev_line, next_line, match = parsed[0]
        first_marker = match.group(0).strip()
        first_tail = first_line[match.end():].strip()

        same_except_number = len(parsed) >= 2 and all(
            self._line_without_chapter_number(line) == self._line_without_chapter_number(parsed[0][0])
            for line, _, _, _ in parsed[1:]
        )

        if same_except_number and next_line:
            return SampleRuleGuess(first_line, next_line, True)

        if first_tail:
            return SampleRuleGuess(first_marker, first_tail, False)

        if next_line:
            return SampleRuleGuess(first_marker, next_line, True)

        return SampleRuleGuess(first_marker, "", False)

    def _sample_marker_match(self, lines: List[str]) -> tuple[Optional[int], Optional[re.Match]]:
        preferred_indexes = []
        if len(lines) >= 3:
            preferred_indexes.append(1)
        preferred_indexes.extend(idx for idx in range(len(lines)) if idx not in preferred_indexes)
        for idx in preferred_indexes:
            match = re.search(self._chapter_marker_match_regex(), lines[idx], re.IGNORECASE)
            if match:
                return idx, match
        return None, None

    def _line_without_chapter_number(self, line: str) -> str:
        return re.sub(self._chapter_marker_match_regex(), "{n}", line.strip(), count=1, flags=re.IGNORECASE)

    def _sample_with_context_lines(self, line_index: int) -> str:
        lines = []
        for prev_index in range(line_index - 1, -1, -1):
            prev_line = self.lines[prev_index].strip()
            if prev_line:
                lines.append(prev_line)
                break
        lines.append(self.lines[line_index].strip())
        for next_index in range(line_index + 1, len(self.lines)):
            next_line = self.lines[next_index].strip()
            if next_line:
                lines.append(next_line)
                break
        return "\n".join(lines)

    def build_regex_from_marker_sample(
        self, marker_sample: str, subtitle_sample: str = "", allow_prefix: bool = False
    ) -> str:
        marker = marker_sample.strip()
        if not marker:
            raise ValueError("분류기호로 사용할 텍스트를 선택하세요. 예: 1화")

        number_match = re.search(r"\d{1,5}", marker)
        if not number_match:
            raise ValueError("분류기호에는 챕터 번호가 포함되어야 합니다. 예: 1화")

        prefix = marker[:number_match.start()]
        suffix = marker[number_match.end():]
        prefix_part = re.escape(prefix).replace(r"\ ", r"\s*")
        suffix_part = re.escape(suffix).replace(r"\ ", r"\s*")

        if prefix_part:
            prefix_part = f"{prefix_part}\\s*"
        if suffix_part:
            suffix_part = f"\\s*{suffix_part}"
        prefix_part = re.sub(r"(?:\\s\*){2,}", r"\\s*", prefix_part)
        suffix_part = re.sub(r"(?:\\s\*){2,}", r"\\s*", suffix_part)

        subtitle_part = r"\s*(?P<subtitle>.*?)" if subtitle_sample.strip() else r".*"
        leading_part = r".*?" if allow_prefix else ""
        if re.fullmatch(r"\d{1,5}", marker):
            marker_part = r"(?P<number>\d{1,5})"
            subtitle_part = r"\s*(?P<subtitle>.*?)" if subtitle_sample.strip() else r"\s*"
        elif marker.lower().startswith("chapter"):
            marker_part = r"chapter\s*(?P<number>\d{1,5})"
        elif marker.lower().startswith("episode"):
            marker_part = r"episode\s*(?P<number>\d{1,5})"
        elif marker.startswith("#"):
            marker_part = r"\#\s*(?P<number>\d{1,5})"
        else:
            marker_part = rf"{prefix_part}(?P<number>\d{{1,5}}){suffix_part}"
        return rf"^\s*{leading_part}(?P<marker>{marker_part}){subtitle_part}\s*$"

    def detect_chapters_by_regex(self, regex: str, max_title_len: int = 80) -> List[Chapter]:
        pattern = re.compile(regex, re.IGNORECASE)
        chapters: List[Chapter] = []

        for i, line in enumerate(self.lines):
            stripped = line.strip()
            if not stripped or len(stripped) > max_title_len:
                continue

            match = pattern.match(stripped)
            if match:
                chapters.append(self._chapter_from_match(match, stripped, i))

        self.current_regex = regex
        self.chapters = self._finalize_chapter_ranges(chapters)
        self.volume_ranges = []
        return self.chapters

    def detect_chapters_by_marker_rules(
        self, regular_regex: str, extra_regex: str = "", max_title_len: int = 80,
        regular_subtitle_from_next_line: bool = False,
        extra_subtitle_from_next_line: bool = False,
        regular_subtitle_prefix: str = "",
        extra_subtitle_prefix: str = "",
    ) -> List[Chapter]:
        regular_chapters = self._detect_chapter_candidates(
            regular_regex, max_title_len, regular_subtitle_from_next_line, regular_subtitle_prefix
        )
        regular_chapters = self._finalize_chapter_ranges(regular_chapters)
        regular_rejected = list(self.rejected_chapter_titles)

        extra_chapters: List[Chapter] = []
        if extra_regex.strip():
            if not regular_chapters:
                raise ValueError("추가/외전 규칙은 정규 챕터가 먼저 인식되어야 사용할 수 있습니다.")
            extra_pattern = re.compile(extra_regex, re.IGNORECASE)
            last_regular = regular_chapters[-1]
            next_number = (last_regular.number or len(regular_chapters)) + 1

            for i, line in enumerate(self.lines):
                if self.line_offsets[i] <= last_regular.start_char:
                    continue
                stripped = line.strip()
                if not stripped or len(stripped) > max_title_len:
                    continue
                match = extra_pattern.match(stripped)
                if match:
                    chapter = self._chapter_from_match(
                        match, stripped, i, extra_subtitle_from_next_line, extra_subtitle_prefix
                    )
                    chapter.number = next_number
                    next_number += 1
                    extra_chapters.append(chapter)

        self.current_regex = regular_regex + ("\n" + extra_regex if extra_regex.strip() else "")
        self.chapters = self._finalize_chapter_ranges(regular_chapters + extra_chapters)
        self.rejected_chapter_titles = regular_rejected + self.rejected_chapter_titles
        self.volume_ranges = []
        return self.chapters

    def _detect_chapter_candidates(
        self, regex: str, max_title_len: int = 80, subtitle_from_next_line: bool = False,
        subtitle_prefix: str = "",
    ) -> List[Chapter]:
        pattern = re.compile(regex, re.IGNORECASE)
        chapters: List[Chapter] = []

        for i, line in enumerate(self.lines):
            stripped = line.strip()
            if not stripped or len(stripped) > max_title_len:
                continue

            match = pattern.match(stripped)
            if match:
                chapters.append(self._chapter_from_match(match, stripped, i, subtitle_from_next_line, subtitle_prefix))

        return chapters

    def add_chapter_by_line(self, line_number: int, title: Optional[str] = None) -> Chapter:
        if line_number < 1 or line_number > len(self.lines):
            raise ValueError("줄 번호가 범위를 벗어났습니다.")
        idx = line_number - 1
        line = self.lines[idx].strip()
        chapter_title = title.strip() if title and title.strip() else line
        if not chapter_title:
            raise ValueError("해당 줄이 비어 있습니다. 제목을 직접 입력하세요.")
        chapter = Chapter(self._extract_number(chapter_title), chapter_title, idx, self.line_offsets[idx], marker=chapter_title)
        self.chapters.append(chapter)
        self.chapters = self._finalize_chapter_ranges(self.chapters)
        self.volume_ranges = []
        return chapter

    def delete_chapter_indexes(self, indexes: List[int]):
        for row in sorted(set(indexes), reverse=True):
            if 0 <= row < len(self.chapters):
                del self.chapters[row]
        self.chapters = self._finalize_chapter_ranges(self.chapters)
        self.volume_ranges = []

    def validate_chapters(self) -> List[str]:
        warnings = []
        nums = [c.number for c in self.chapters if isinstance(c.number, int)]

        if not self.chapters:
            return ["챕터가 인식되지 않았습니다."]

        if self.rejected_chapter_titles:
            warnings.append(f"역순/중복으로 제외된 챕터 후보: {len(self.rejected_chapter_titles)}개")

        if len(nums) >= 2:
            seen = set()
            duplicates = []
            for n in nums:
                if n in seen:
                    duplicates.append(n)
                seen.add(n)
            if duplicates:
                warnings.append(f"중복 챕터 번호: {sorted(set(duplicates))}")

            min_n, max_n = min(nums), max(nums)
            missing = [n for n in range(min_n, max_n + 1) if n not in seen]
            if missing:
                warnings.append(f"누락 의심 챕터: {missing}")

            for prev, cur in zip(nums, nums[1:]):
                if cur < prev:
                    warnings.append(f"번호 역순 의심: {prev} 다음에 {cur}")
                if cur - prev > 1:
                    warnings.append(f"번호 점프 의심: {prev} → {cur}")

        for a, b in zip(self.chapters, self.chapters[1:]):
            if b.line_index - a.line_index <= 2:
                warnings.append(f"챕터 간격이 너무 짧음: {a.title} → {b.title}")
                break

        return warnings or ["검증 결과: 큰 문제 없음"]

    def get_chapter_body(self, chapter: Chapter) -> str:
        if chapter.edited_body is not None:
            return self._edited_chapter_body_for_current_delete_lines(chapter).strip()
        return self.get_original_chapter_body(chapter)

    def get_original_chapter_body(self, chapter: Chapter) -> str:
        body_start, body_end = self.get_chapter_body_range(chapter)
        return self._remove_ignored_body_lines(body_start, body_end).strip()

    def join_text_for_merge(self, *parts: str) -> str:
        cleaned = []
        for part in parts:
            value = (part or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if value:
                cleaned.append(value)
        return "\n".join(cleaned)

    def get_chapter_full_text_for_merge(self, chapter: Chapter) -> str:
        if chapter.edited_body is None:
            return self.text[chapter.start_char:chapter.end_char].strip()

        title_start = chapter.start_char
        body_start, _ = self.get_chapter_body_range(chapter)
        heading_text = self.text[title_start:body_start].strip()
        body_text = self._edited_chapter_body_for_delete_lines(chapter, 0).strip()
        return self.join_text_for_merge(heading_text, body_text)

    def _edited_chapter_body_for_current_delete_lines(self, chapter: Chapter) -> str:
        return self._edited_chapter_body_for_delete_lines(chapter, chapter.manual_delete_lines)

    def _edited_chapter_body_for_delete_lines(self, chapter: Chapter, delete_lines: int) -> str:
        text = chapter.edited_body or ""
        if not chapter.manual_body_start:
            return text

        saved_delete_lines = max(0, chapter.edited_body_delete_lines)
        current_delete_lines = max(0, delete_lines)
        if current_delete_lines == saved_delete_lines:
            return text
        if current_delete_lines > saved_delete_lines:
            return self._drop_leading_text_lines(text, current_delete_lines - saved_delete_lines)

        prefix = self._manual_original_lines_text(
            chapter,
            current_delete_lines,
            saved_delete_lines,
        )
        return prefix + text

    def _drop_leading_text_lines(self, text: str, line_count: int) -> str:
        for _ in range(max(0, line_count)):
            match = re.search(r"\r\n|\n|\r", text)
            if not match:
                return ""
            text = text[match.end():]
        return text

    def _manual_original_lines_text(self, chapter: Chapter, start_line_offset: int, end_line_offset: int) -> str:
        start_line = chapter.line_index + max(0, start_line_offset)
        end_line = min(chapter.line_index + max(0, end_line_offset), len(self.line_offsets))
        if start_line >= end_line or start_line >= len(self.line_offsets):
            return ""

        start_char = self.line_offsets[start_line]
        end_char = self.line_offsets[end_line] if end_line < len(self.line_offsets) else len(self.text)
        end_char = min(end_char, chapter.end_char)
        return self.text[start_char:end_char]

    def get_chapter_body_range(self, chapter: Chapter) -> tuple[int, int]:
        if chapter.manual_body_start:
            body_start = chapter.start_char
        else:
            title_line = self.lines[chapter.line_index] if chapter.line_index < len(self.lines) else ""
            body_start = chapter.start_char + len(title_line)
        while body_start < chapter.end_char and self.text[body_start] in "\r\n":
            body_start += 1
        if chapter.subtitle_line_index is not None:
            subtitle_line = self.lines[chapter.subtitle_line_index] if chapter.subtitle_line_index < len(self.lines) else ""
            subtitle_end = self.line_offsets[chapter.subtitle_line_index] + len(subtitle_line)
            if body_start <= self.line_offsets[chapter.subtitle_line_index] < chapter.end_char:
                body_start = subtitle_end
                while body_start < chapter.end_char and self.text[body_start] in "\r\n":
                    body_start += 1
        return body_start, chapter.end_char

    def _remove_ignored_body_lines(self, start_char: int, end_char: int) -> str:
        ignored = [
            idx for idx in sorted(self.ignored_body_line_indexes)
            if idx < len(self.line_offsets) and start_char <= self.line_offsets[idx] < end_char
        ]
        if not ignored:
            return self.text[start_char:end_char]

        parts = []
        pos = start_char
        for idx in ignored:
            line_start = self.line_offsets[idx]
            next_start = self.line_offsets[idx + 1] if idx + 1 < len(self.line_offsets) else len(self.text)
            parts.append(self.text[pos:line_start])
            pos = min(next_start, end_char)
        parts.append(self.text[pos:end_char])
        return "".join(parts)

    def line_index_from_char(self, char_pos: int) -> int:
        if not self.line_offsets:
            return 0
        lo, hi = 0, len(self.line_offsets) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.line_offsets[mid] <= char_pos:
                lo = mid + 1
            else:
                hi = mid - 1
        return max(0, hi)

    def insert_chapter_at_line(self, line_index: int, title: str, subtitle: str = "") -> Chapter:
        if not (0 <= line_index < len(self.lines)):
            raise ValueError("분할 위치가 원문 범위를 벗어났습니다.")
        marker = self._normalize_marker_for_title(title.strip())
        clean_subtitle = subtitle.strip()
        chapter_title = self._format_chapter_title(marker, clean_subtitle, title)
        chapter = Chapter(
            self._extract_number(marker),
            chapter_title,
            line_index,
            self.line_offsets[line_index],
            marker=marker,
            subtitle=clean_subtitle,
        )
        self.chapters.append(chapter)
        self.chapters = self._finalize_chapter_ranges(self.chapters)
        self.volume_ranges = []
        return chapter

    def make_volume_ranges(
        self,
        chapters_per_volume: int,
        from_volume: int = 0,
        after_chapters_per_volume: int = 0,
        second_from_volume: int = 0,
        second_after_chapters_per_volume: int = 0,
    ) -> List[VolumeRange]:
        if chapters_per_volume <= 0:
            raise ValueError("권당 챕터 수는 1 이상이어야 합니다.")
        if after_chapters_per_volume < 0:
            raise ValueError("이후 권당 챕터 수는 0 이상이어야 합니다.")
        if second_after_chapters_per_volume < 0:
            raise ValueError("2차 이후 권당 챕터 수는 0 이상이어야 합니다.")
        if not self.chapters:
            raise ValueError("챕터를 먼저 인식하세요.")
        ranges = []
        vol = 1
        start = 0
        total = len(self.chapters)
        while start < total:
            current_size = chapters_per_volume
            if after_chapters_per_volume > 0 and from_volume > 0 and vol >= from_volume:
                current_size = after_chapters_per_volume
            if second_after_chapters_per_volume > 0 and second_from_volume > 0 and vol >= second_from_volume:
                current_size = second_after_chapters_per_volume
            end = self._volume_end_index_excluding_zero_chapters(start, current_size)
            ranges.append(VolumeRange(vol, start, end))
            start = end + 1
            vol += 1
        self.volume_ranges = ranges
        return ranges

    def _volume_end_index_excluding_zero_chapters(self, start: int, chapter_count: int) -> int:
        counted = 0
        idx = start
        last_index = len(self.chapters) - 1
        while idx <= last_index:
            chapter = self.chapters[idx]
            if chapter.number != 0:
                counted += 1
            if counted >= chapter_count:
                return idx
            idx += 1
        return last_index

    def create_epub(
        self, output_path: str, title: str, creator: str, publisher: str = "",
        language: str = "ko", isbn: str = "", subtitle: str = "",
        translator: str = "", date: str = "", series: str = "",
        ebook_publisher: str = "",
        volume_no: str = "", description: str = "", rights: str = "",
        total_volumes: str = "", total_episodes: str = "", episode_range: str = "",
        chapter_subset: Optional[List[Chapter]] = None,
        volume_end_text: str = "",
        completed: bool = True,
    ):
        chapters = chapter_subset if chapter_subset is not None else self.chapters
        if not self.text:
            raise ValueError("TXT 파일을 먼저 불러오세요.")
        if not chapters:
            raise ValueError("챕터를 먼저 인식하세요.")
        self._ensure_chapter_order(chapters)

        book = epub.EpubBook()
        book.FOLDER_NAME = "OEBPS"
        book.set_identifier(isbn.strip() or str(uuid.uuid4()))
        final_title = title.strip() or self._default_title()
        if volume_no.strip():
            final_title = f"{final_title} {volume_no.strip()}권"
        book.set_title(final_title)
        book.set_language(language.strip() or "ko")

        if creator.strip():
            book.add_author(creator.strip())
        if translator.strip():
            book.add_metadata("DC", "contributor", translator.strip(), {"role": "translator"})
        if publisher.strip():
            book.add_metadata("DC", "publisher", publisher.strip())
        if isbn.strip():
            book.add_metadata("DC", "identifier", isbn.strip(), {"id": "isbn"})
        if date.strip():
            book.add_metadata("DC", "date", date.strip())
        display_episode_range = episode_range if volume_no.strip() else ""
        for label, value in self._episode_meta_lines(total_volumes, total_episodes, display_episode_range):
            book.add_metadata("DC", "description", f"{label}: {value}")
        if rights.strip():
            book.add_metadata("DC", "rights", rights.strip())
        if subtitle.strip():
            book.add_metadata("DC", "description", f"부제: {subtitle.strip()}")
        if series.strip():
            book.add_metadata("OPF", "meta", series.strip(), {"name": "calibre:series"})

        has_cover = False
        if self.cover_path and os.path.exists(self.cover_path):
            cover_name, cover_bytes = self._load_cover_for_epub(self.cover_path)
            book.set_cover(cover_name, cover_bytes)
            has_cover = True

        font_path = self._font_path()
        has_font = bool(font_path and os.path.exists(font_path))
        font_family = "BookBodyFont"
        font_file_name = self._font_epub_file_name(font_path) if has_font else ""
        font_media_type = self._font_media_type(font_path) if has_font else ""
        font_face_css = (
            f"""
@font-face {{
    font-family: "{font_family}";
    src: url("../fonts/{font_file_name}");
    font-weight: normal;
    font-style: normal;
}}
""".strip()
            + "\n"
            if has_font else ""
        )
        body_font_css = f'font-family: "{font_family}", serif; ' if has_font else ""
        font_style = f" font-family: '{font_family}', serif;" if has_font else ""
        
        # CSS 파일 생성 제거 (인라인 스타일 및 태그 사용으로 대체)
        if has_font:
            with open(font_path, "rb") as f:
                book.add_item(
                    epub.EpubItem(
                        uid="font_body",
                        file_name=f"fonts/{font_file_name}",
                        media_type=font_media_type,
                        content=f.read(),
                    )
                )

        epub_chapters = []
        used_chapter_file_names: set[str] = set()
        for idx, chapter in enumerate(chapters, start=1):
            chapter_text = self.get_chapter_body(chapter)
            closing_text = volume_end_text if idx == len(chapters) else ""
            item = epub.EpubHtml(
                title=chapter.title,
                file_name=self._chapter_epub_file_name(chapter, idx, used_chapter_file_names),
                lang=language.strip() or "ko",
            )
            item.content = self._chapter_to_xhtml(
                chapter.title,
                chapter_text,
                marker=chapter.marker,
                subtitle=self._join_subtitle_prefix(chapter.subtitle_prefix, chapter.subtitle),
                closing_text=closing_text,
                font_style=font_style,
            )
            book.add_item(item)
            epub_chapters.append(item)

        copyright_item = epub.EpubHtml(
            title="판권",
            file_name="text/copyright.xhtml",
            lang=language.strip() or "ko",
        )
        copyright_item.content = self._copyright_to_xhtml(
            final_title=final_title,
            subtitle=subtitle,
            creator=creator,
            translator=translator,
            publisher=publisher,
            date=date,
            ebook_publisher=ebook_publisher,
            isbn=isbn,
            series=series,
            rights=rights,
            total_volumes=total_volumes,
            total_episodes=total_episodes,
            episode_range=display_episode_range,
            font_style=font_style,
        )
        book.add_item(copyright_item)
        
        # 목차 페이지 생성
        toc_item = epub.EpubHtml(
            title="목차",
            file_name="text/toc.xhtml",
            lang=language.strip() or "ko",
        )
        toc_item.content = self._toc_to_xhtml(
            final_title=final_title,
            chapters=epub_chapters,
            copyright_item=copyright_item,
            font_style=font_style,
        )
        book.add_item(toc_item)
        
        reading_order = [toc_item] + list(epub_chapters) + [copyright_item]

        book.toc = tuple([toc_item] + epub_chapters + [copyright_item])
        book.spine = (["cover"] if has_cover else []) + reading_order
        book.add_item(epub.EpubNcx())
        write_epub2(output_path, book, {})

    def create_volume_epubs(self, output_dir: str, meta: dict):
        if not self.volume_ranges:
            raise ValueError("권 분할표를 먼저 생성하세요.")
        base_title = meta.get("title", "") or self._default_title()
        created = []
        for vr in self.volume_ranges:
            subset = self.chapters[vr.start_index : vr.end_index + 1]
            meta2 = dict(meta)
            meta2["volume_no"] = str(vr.volume)
            meta2["total_volumes"] = str(len(self.volume_ranges))
            meta2["episode_range"] = self._episode_range_from_chapters(subset)
            is_last_volume = vr.volume == len(self.volume_ranges)
            meta2["volume_end_text"] = "< 끝 >" if is_last_volume else "< 다음권에 계속 >"
            file_name = self.volume_epub_filename(base_title, meta.get("creator", ""), vr.volume, bool(meta.get("completed", True)) and is_last_volume)
            output_path = os.path.join(output_dir, file_name)

            self.create_epub(output_path=output_path, chapter_subset=subset, **meta2)
            created.append(output_path)
        return created

    def volume_epub_filename(self, title: str, creator: str, volume: int, completed: bool = False) -> str:
        name = self.book_filename_base(title, creator)
        suffix = " 完" if completed else ""
        return f"{self._safe_filename(f'{name} {volume}권_made by abh{suffix}')}.epub"

    def single_epub_filename(self, title: str, creator: str, episode_range: str = "", completed: bool = True) -> str:
        name = self.book_filename_base(title, creator)
        range_text = self._normalize_episode_range_for_filename(episode_range)
        suffix = " 完" if completed else ""
        if range_text:
            name = f"{name} {range_text}"
        return f"{self._safe_filename(name + '_made by abh' + suffix)}.epub"

    def book_filename_base(self, title: str, creator: str) -> str:
        title = (title or self._default_title()).strip()
        creator = (creator or "").strip()
        return f"{title} [{creator}]" if creator else title

    def _chapter_epub_file_name(self, chapter: Chapter, index: int, used_names: set[str]) -> str:
        number = chapter.number if isinstance(chapter.number, int) and chapter.number > 0 else index
        base = f"text/chapter_{number:04d}"
        name = f"{base}.xhtml"
        suffix = 2
        while name in used_names:
            name = f"{base}_{suffix:02d}.xhtml"
            suffix += 1
        used_names.add(name)
        return name

    def _normalize_episode_range_for_filename(self, episode_range: str) -> str:
        episode_range = (episode_range or "").strip()
        match = re.search(r"(\d{1,5})\s*[-~]\s*(\d{1,5})\s*화?", episode_range)
        if match:
            return f"{int(match.group(1))}-{int(match.group(2))}화"
        match = re.search(r"(\d{1,5})\s*화", episode_range)
        if match:
            return f"{int(match.group(1))}화"
        return episode_range

    def _chapter_heading_xhtml(self, title: str, marker: str = "", subtitle: str = "") -> str:
        marker, subtitle = self._split_chapter_heading_parts(title, marker, subtitle)
        if marker:
            marker_html = html.escape(marker)
            # 화수가 있고 부제가 있는 경우: 화수, 부제 (가운데), 사이 <br> 없음
            if subtitle:
                subtitle_html = f'\n        <h3 align="center">{html.escape(subtitle)}</h3>'
                return (
                    '<div>\n'
                    f'        <h2>{marker_html}</h2>'
                    f'{subtitle_html}\n'
                    '        <br>\n'
                    '    </div>'
                )
            else:
                # 화수만 있는 경우
                return (
                    '<div>\n'
                    f'        <h2>{marker_html}</h2>\n'
                    '        <br>\n'
                    '    </div>'
                )
        
        # 마커가 없고 제목만 있는 경우 (기존 처리)
        if subtitle:
             return (
                '<div>\n'
                f'        <h2>{html.escape(title)}</h2>'
                f'\n        <h3 align="center">{html.escape(subtitle)}</h3>\n'
                '        <br>\n'
                '    </div>'
             )
        
        # 제목만 있는 경우
        return (
            '<div>\n'
            f'        <h2>{html.escape(title)}</h2>\n'
            '        <br>\n'
            '    </div>'
        )

    def _split_chapter_heading_parts(self, title: str, marker: str = "", subtitle: str = "") -> tuple[str, str]:
        marker = (marker or "").strip()
        subtitle = (subtitle or "").strip()
        if marker and subtitle:
            return marker, subtitle
        if marker:
            return marker, subtitle

        source = marker or title
        match = re.match(
            r"^\s*(?P<marker>(?:제\s*)?\d{1,5}\s*(?:화|장|회|차)\s*[\.\)\]]?|chapter\s*\d{1,5})(?:\s+|$)(?P<subtitle>.*?)\s*$",
            source,
            re.IGNORECASE,
        )
        if match:
            parsed_marker = self._normalize_marker_for_title(match.group("marker"))
            parsed_subtitle = subtitle or match.group("subtitle").strip()
            return parsed_marker, parsed_subtitle

        return marker, subtitle

    def _copyright_to_xhtml(
        self, final_title: str, subtitle: str, creator: str, translator: str, publisher: str,
        date: str, ebook_publisher: str, isbn: str, series: str, rights: str,
        total_volumes: str = "", total_episodes: str = "", episode_range: str = "",
        font_style: str = "",
    ) -> str:
        lines = [
            ("제목", final_title),
            ("부제", subtitle),
            ("전자책 발행일", self._today_text()),
            ("전자책 발행인", ebook_publisher),
            ("지은이", creator),
            ("역자", translator),
            ("펴낸곳", publisher),
            ("발행일", date),
            ("ISBN", isbn),
            ("시리즈", series),
            ("총 권수", total_volumes),
            ("총 화수", total_episodes),
            ("수록 화수", episode_range),
            ("판권/저작권", rights),
        ]
        body_lines = []
        for label, value in lines:
            value = value.strip()
            if value:
                body_lines.append(f"{label}: {value}")
        if not body_lines:
            body_lines.append("판권 정보 없음")
        return self._copyright_page_xhtml(final_title, body_lines, font_style)

    def _copyright_page_xhtml(self, final_title: str, body_lines: List[str], font_style: str = "") -> str:
        values = {}
        note = ""
        label_map = {
            "총 권수": "총 권수",
            "총 화수": "총 화수",
            "수록 화수": "수록 화수",
        }
        for line in body_lines:
            if ":" in line:
                label, value = line.split(":", 1)
                if label == "판권/저작권":
                    note = value.strip()
                    continue
                label = label.strip()
                values[label_map.get(label, label)] = value.strip()
            else:
                note = line.strip()
        issue_lines = []
        for label in ["전자책 발행일", "전자책 발행인"]:
            value = values.get(label, "")
            if value:
                if label == "전자책 발행일":
                    value = self._format_korean_date(value)
                issue_lines.append(
                    f'<p><strong>{html.escape(label)}</strong> │ {html.escape(value)}</p>'
                )

        creator_lines = []
        for label in ["지은이", "역자", "펴낸곳", "발행일"]:
            value = values.get(label, "")
            if value:
                if label == "발행일":
                    value = self._format_korean_date(value)
                display_label = "옮긴이" if label == "역자" else label
                creator_lines.append(
                    f'<p><strong>{html.escape(display_label)}</strong> │ {html.escape(value)}</p>'
                )
        volume_lines = []
        for label in ["총 권수", "총 화수"]:
            value = values.get(label, "")
            if value:
                value = self._format_episode_value(label, value)
                volume_lines.append(
                    f'<p><strong>{html.escape(label)}</strong> │ {html.escape(value)}</p>'
                )
        episode_range = values.get("수록 화수", "")
        if episode_range:
            volume_lines.append(
                f'<p><strong>수록 화수</strong> │ {html.escape(self._format_episode_value("수록 화수", episode_range))}</p>'
            )
        issue_block = "\n        ".join(issue_lines)
        creator_block = "\n        ".join(creator_lines)
        volume_block = "\n        ".join(volume_lines)
        series = values.get("시리즈", "")
        series_html = f'\n        <p>{html.escape(series)}</p>' if series else ""
        subtitle = values.get("부제", "")
        subtitle_html = f'\n        <h3 align="center">{html.escape(subtitle)}</h3>\n        <br>' if subtitle else ""
        isbn = values.get("ISBN", "")
        isbn_html = f'\n        <p>ISBN {html.escape(isbn)}</p>' if isbn else ""
        note_html = f'\n        <p>&#160;</p>\n        <p>{html.escape(note)}</p>' if note else ""
        return f"""
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <title>판권</title>
</head>
<body style="line-height: 1.8; margin: 5%;{font_style}">
    <div>
        {series_html}
        <h1>{html.escape(final_title)}</h1>
        <br>{subtitle_html}
        <p>&#160;</p>
        {issue_block}
        <p>&#160;</p>
        {creator_block}
        <p>&#160;</p>
        {volume_block}
        <p>&#160;</p>{isbn_html}{note_html}
    </div>
</body>
</html>
""".strip()

    def _today_text(self) -> str:
        today = date.today()
        return f"{today.year}년 {today.month}월 {today.day}일"

    def _format_korean_date(self, value: str) -> str:
        value = value.strip()
        match = re.match(r"^(\d{4})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})(?:일)?\.?$", value)
        if not match:
            return value
        year, month, day = (int(part) for part in match.groups())
        return f"{year}년 {month}월 {day}일"

    def _format_episode_value(self, label: str, value: str) -> str:
        value = value.strip()
        if label == "총 권수":
            match = re.search(r"\d+", value)
            return f"{match.group(0)} 권" if match else value
        if label == "총 화수":
            match = re.search(r"\d+", value)
            return f"{match.group(0)} 화" if match else value
        if label == "수록 화수":
            match = re.search(r"(\d{1,5})\s*[-~]\s*(\d{1,5})", value)
            if match:
                return f"{int(match.group(1))} ~ {int(match.group(2))} 화"
            match = re.search(r"\d+", value)
            return f"{match.group(0)} 화" if match else value
        return value

    def _episode_meta_lines(self, total_volumes: str, total_episodes: str, episode_range: str) -> List[tuple[str, str]]:
        return [
            (label, value.strip())
            for label, value in [
                ("총 권수", total_volumes or ""),
                ("총 화수", total_episodes or ""),
                ("수록 화수", episode_range or ""),
            ]
            if value and value.strip()
        ]

    def _episode_range_from_chapters(self, chapters: List[Chapter]) -> str:
        numbers = [chapter.number for chapter in chapters if isinstance(chapter.number, int)]
        if not numbers:
            return ""
        start = min(numbers)
        end = max(numbers)
        return f"{start}화" if start == end else f"{start}-{end}화"

    def _load_cover_for_epub(self, cover_path: str) -> tuple[str, bytes]:
        image = QImage(cover_path)
        if image.isNull():
            raise ValueError("표지를 JPG로 변환하지 못했습니다.")
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        if not image.save(buffer, "JPG", 92):
            raise ValueError("표지를 JPG로 저장하지 못했습니다.")
        buffer.close()
        return "cover.jpg", bytes(data)

    def _font_path(self) -> str:
        if self.font_path and os.path.exists(self.font_path):
            return self.font_path
        return ""

    def _font_epub_file_name(self, path: str) -> str:
        if not path:
            return "body-font.ttf"
        ext = os.path.splitext(path)[1].lower() or ".ttf"
        return f"body-font{ext}"

    def _font_media_type(self, path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".otf":
            return "font/otf"
        if ext == ".woff":
            return "font/woff"
        if ext == ".woff2":
            return "font/woff2"
        return "font/ttf"

    def _toc_to_xhtml(
        self, final_title: str, chapters: List, copyright_item, font_style: str = ""
    ) -> str:
        lines = [f'<h1 align="center">{html.escape(final_title)}</h1>', "<br>"]
        
        for idx, chapter in enumerate(chapters, start=1):
            chapter_href = chapter.file_name
            title_text = chapter.title
            
            # 부제 추출 (있을 경우)
            subtitle_text = getattr(chapter, 'subtitle', '')
            if not subtitle_text:
                # marker 와 subtitle 분리 시도
                marker, subtitle = self._split_chapter_heading_parts(chapter.title, getattr(chapter, 'marker', ''), '')
                if subtitle:
                    subtitle_text = subtitle
            
            if subtitle_text:
                # 화수와 부제를 한 줄에 표시
                lines.append(f'<p><a href="{chapter_href}">{html.escape(title_text)} - {html.escape(subtitle_text)}</a></p>')
            else:
                lines.append(f'<p><a href="{chapter_href}">{html.escape(title_text)}</a></p>')
            
            # 10 화마다 빈줄 추가
            if idx % 10 == 0:
                lines.append("<br>")
        
        # 마지막에 빈줄 추가
        lines.append("<br>")
        
        # 판권 페이지 링크 추가
        lines.append(f'<p><a href="{copyright_item.file_name}">판권정보</a></p>')
        
        body_content = "\n".join(lines)
        
        return f"""
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <title>목차</title>
</head>
<body style="line-height: 1.8; margin: 5%;{font_style}">
    <div>
        {body_content}
    </div>
</body>
</html>
""".strip()

    def _chapter_epub_file_name(self, chapter: Chapter, index: int, used_names: set[str]) -> str:
        number = chapter.number if isinstance(chapter.number, int) and chapter.number > 0 else index
        base = f"text/chapter_{number:04d}"
        name = f"{base}.xhtml"
        suffix = 2
        while name in used_names:
            name = f"{base}_{suffix:02d}.xhtml"
            suffix += 1
        used_names.add(name)
        return name

    def _chapter_to_xhtml(
        self, title: str, text: str, marker: str = "", subtitle: str = "", closing_text: str = "",
        font_style: str = "",
    ) -> str:
        escaped_title = html.escape(title)
        heading = self._chapter_heading_xhtml(title, marker, subtitle)
        text = self._normalize_blank_lines(text)
        paragraphs = []
        for block in text.split("\n"):
            paragraphs.append(f"<p>{html.escape(block.strip())}</p>" if block.strip() else "<p>&#160;</p>")
        if closing_text.strip():
            paragraphs.append("<p>&#160;</p>")
            paragraphs.append(
                f'<p align="right">'
                f'{html.escape(closing_text.strip())}</p>'
            )
        body = "\n".join(paragraphs)
        return f"""
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <title>{escaped_title}</title>
</head>
<body style="line-height: 1.8; margin: 5%;{font_style}">
    {heading}
    <p>&#160;</p>
    <div>
    {body}
    </div>
</body>
</html>
""".strip()

    def _normalize_blank_lines(self, text: str) -> str:
        raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        normalized = []
        blank_count = 0

        for line in raw_lines:
            if line.strip():
                if blank_count >= 2 and normalized:
                    normalized.append("")
                normalized.append(line)
                blank_count = 0
            else:
                blank_count += 1

        return "\n".join(normalized).strip()

    def _finalize_chapter_ranges(self, chapters: List[Chapter]) -> List[Chapter]:
        chapters = sorted(chapters, key=lambda c: c.start_char)
        filtered = []
        previous_number = None
        self.rejected_chapter_titles = []

        for chapter in chapters:
            if isinstance(chapter.number, int):
                if previous_number is not None and chapter.number <= previous_number:
                    self.rejected_chapter_titles.append(chapter.title)
                    continue
                previous_number = chapter.number
            filtered.append(chapter)

        for i, chapter in enumerate(filtered):
            chapter.end_char = filtered[i + 1].start_char if i + 1 < len(filtered) else len(self.text)
        return filtered

    def _ensure_chapter_order(self, chapters: List[Chapter]):
        previous = None
        for chapter in chapters:
            if not isinstance(chapter.number, int):
                continue
            if previous is not None and chapter.number <= previous.number:
                raise ValueError(
                    f"챕터 번호 역순/중복은 허용되지 않습니다: "
                    f"{previous.title} 다음에 {chapter.title}"
                )
            previous = chapter

    def _extract_number(self, value) -> Optional[int]:
        if value is None:
            return None
        m = re.search(r"\d+", str(value))
        return int(m.group(0)) if m else None

    def _extract_number_from_match(self, match: re.Match) -> Optional[int]:
        if "number" in match.groupdict():
            number = self._extract_number(match.group("number"))
            if number is not None:
                return number
        for group in match.groups():
            number = self._extract_number(group)
            if number is not None:
                return number
        return self._extract_number(match.group(0))

    def _chapter_from_match(
        self, match: re.Match, title: str, line_index: int, subtitle_from_next_line: bool = False,
        subtitle_prefix: str = "",
    ) -> Chapter:
        groups = match.groupdict()
        marker = (groups.get("marker") or "").strip()
        subtitle = (groups.get("subtitle") or "").strip()
        subtitle_line_index = None
        if subtitle_from_next_line:
            next_line_index, next_line = self._next_content_line(line_index)
            if next_line:
                subtitle = next_line
                subtitle_line_index = next_line_index
        elif "subtitle" in groups:
            subtitle = self._subtitle_after_marker_chunk(match, title, subtitle)
        number = self._extract_number_from_match(match)
        marker = self._normalize_marker_for_title(marker)
        chapter_title = self._format_chapter_title(marker, subtitle, title, subtitle_prefix)
        return Chapter(
            number, chapter_title, line_index, self.line_offsets[line_index],
            marker=marker, subtitle=subtitle, subtitle_line_index=subtitle_line_index,
            subtitle_prefix=subtitle_prefix.strip(),
        )

    def _subtitle_after_marker_chunk(self, match: re.Match, title: str, fallback: str) -> str:
        try:
            marker_start, marker_end = match.span("marker")
        except IndexError:
            return fallback

        if marker_start < 0 or marker_end < 0:
            return fallback

        chunk_start = marker_start
        while chunk_start > 0 and not title[chunk_start - 1].isspace():
            chunk_start -= 1

        chunk_end = marker_end
        while chunk_end < len(title) and not title[chunk_end].isspace():
            chunk_end += 1

        marker_chunk = title[chunk_start:chunk_end]
        if not re.search(r"\d{1,5}", marker_chunk):
            return fallback

        subtitle = title[chunk_end:].strip()
        return subtitle

    def _format_chapter_title(self, marker: str, subtitle: str, fallback: str, subtitle_prefix: str = "") -> str:
        marker = self._normalize_marker_for_title(marker)
        subtitle = self._join_subtitle_prefix(subtitle_prefix, subtitle)
        if marker and subtitle:
            return f"{marker} {subtitle}"
        if marker:
            return marker
        return fallback.strip()

    def _join_subtitle_prefix(self, prefix: str, subtitle: str) -> str:
        prefix = prefix.strip()
        subtitle = subtitle.strip()
        if prefix and subtitle:
            return f"{prefix} {subtitle}"
        return prefix or subtitle

    def _episode_marker_from_number(self, number: Optional[int]) -> str:
        return f"{number}화" if isinstance(number, int) else ""

    def _normalize_marker_for_title(self, marker: str) -> str:
        marker = marker.strip()
        number = self._extract_number(marker)
        if number is not None:
            return f"{number}화"
        return marker

    def _next_content_line(self, line_index: int) -> tuple[Optional[int], str]:
        for next_index in range(line_index + 1, len(self.lines)):
            next_line = self.lines[next_index].strip()
            if next_line:
                return next_index, next_line
        return None, ""

    def _default_title(self) -> str:
        return os.path.splitext(os.path.basename(self.txt_path))[0] if self.txt_path else "Untitled"

    def _safe_filename(self, name: str) -> str:
        return re.sub(r"[\\/:*?\"<>|]", "_", name).strip() or "output"

    def parse_title_author_from_filename(self, path: str) -> tuple[str, str]:
        filename = os.path.splitext(os.path.basename(path))[0].strip()
        match = re.match(r"^(?P<title>.*?)\s*\[(?P<author>[^\]]+)\]", filename)
        if match:
            title = match.group("title").strip()
            author = match.group("author").strip()
            return title or filename, author
        return filename, ""

    def cover_search_key_from_txt(self, path: str) -> str:
        filename = os.path.splitext(os.path.basename(path))[0].strip()
        match = re.match(r"^(?P<key>.*?\[[^\]]+\])", filename)
        return (match.group("key") if match else filename).strip()

    def find_cover_for_txt(self, path: str) -> str:
        folder = os.path.dirname(os.path.abspath(path))
        key = self.cover_search_key_from_txt(path).casefold()
        image_exts = {".jpg", ".jpeg", ".png", ".webp"}
        if not os.path.isdir(folder) or not key:
            return ""
        for name in os.listdir(folder):
            full_path = os.path.join(folder, name)
            base, ext = os.path.splitext(name)
            if os.path.isfile(full_path) and ext.lower() in image_exts and key in base.casefold():
                return full_path
        return ""

    def search_isbn_records(self, query: str, limit: int = 20) -> List[IsbnRecord]:
        return self.search_isbn_page(query, page=1, page_unit=limit).records

    def search_isbn_page(self, query: str, page: int = 1, page_unit: int = 10) -> IsbnSearchResult:
        query = query.strip()
        if not query:
            raise ValueError("검색어를 입력하세요.")
        page = max(1, page)
        page_unit = max(1, page_unit)

        html_text = self._fetch_nl_html(
            self.isbn_search_url(query, page, page_unit),
            "https://www.nl.go.kr/seoji/",
        )
        return IsbnSearchResult(
            records=self._parse_isbn_results(html_text)[:page_unit],
            current_page=page,
            total_count=self._parse_isbn_total_count(html_text),
            page_unit=page_unit,
        )

    def isbn_search_url(self, query: str, page: int = 1, page_unit: int = 10) -> str:
        url = "https://www.nl.go.kr/seoji/contents/S80100000000.do"
        query_string = urllib.parse.urlencode({
            "schType": "simple",
            "schFld": "title",
            "schStr": query.strip(),
            "page": str(max(1, page)),
            "pageUnit": str(max(1, page_unit)),
        })
        return f"{url}?{query_string}"

    def fetch_isbn_detail(self, record: IsbnRecord, query: str = "") -> IsbnRecord:
        if not record.detail_isbn:
            return record
        url = "https://www.nl.go.kr/seoji/contents/S80100000000.do"
        query_string = urllib.parse.urlencode({
            "schM": "intgr_detail_view_isbn",
            "schType": "simple",
            "schFld": "title",
            "schStr": query or record.title,
            "page": "1",
            "pageUnit": "10",
            "isbn": record.detail_isbn,
            "cipId": record.cip_id,
        })
        html_text = self._fetch_nl_html(f"{url}?{query_string}", url)
        detail = self._parse_isbn_detail(html_text)
        for key, value in detail.__dict__.items():
            if value:
                setattr(record, key, value)
        return record

    def _fetch_nl_html(self, url: str, referer: str) -> str:
        command = """
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
try {
    Invoke-WebRequest -Uri 'https://www.nl.go.kr/seoji/' -WebSession $session -UseBasicParsing -TimeoutSec 20 | Out-Null
} catch {}
$url = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($env:NL_URL_B64))
$referer = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($env:NL_REFERER_B64))
$response = Invoke-WebRequest -Uri $url -WebSession $session -Headers @{ Referer = $referer } -UseBasicParsing -TimeoutSec 45
$response.Content
""".strip()
        env = os.environ.copy()
        env["NL_URL_B64"] = base64.b64encode(url.encode("utf-8")).decode("ascii")
        env["NL_REFERER_B64"] = base64.b64encode(referer.encode("utf-8")).decode("ascii")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=70,
        )
        if result.returncode != 0 and not result.stdout.strip():
            raise ValueError(result.stderr.strip() or "국립중앙도서관 검색 요청에 실패했습니다.")
        return result.stdout

    def _parse_isbn_results(self, html_text: str) -> List[IsbnRecord]:
        records = []
        for block in re.findall(r'<div class="resultData">(.*?)</div>\s*</div>\s*</div>', html_text, re.DOTALL):
            onclick = re.search(r"fn_goView\('([^']*)','([^']*)'\)", block)
            title_match = re.search(r'<div class="tit">\s*<a[^>]*>(.*?)</a>', block, re.DOTALL)
            if not title_match:
                continue
            record = IsbnRecord(
                title=self._clean_book_title(self._html_to_text(title_match.group(1))),
                detail_isbn=onclick.group(1) if onclick else "",
                cip_id=onclick.group(2) if onclick else "",
            )
            for item in re.findall(r"<li>(.*?)</li>", block, re.DOTALL):
                text = self._html_to_text(item)
                self._fill_isbn_record_field(record, text)
            records.append(record)
        return records

    def _parse_isbn_total_count(self, html_text: str) -> int:
        match = re.search(r'totCnt_span"[^>]*>\s*([\d,]+)\s*</span>', html_text)
        if not match:
            return 0
        return int(match.group(1).replace(",", ""))

    def _parse_isbn_detail(self, html_text: str) -> IsbnRecord:
        record = IsbnRecord(title="")
        detail_start = html_text.find('<div class="resultViewDetail">')
        if detail_start < 0:
            return record
        detail_html = html_text[detail_start:]
        title_match = re.search(r'<div class="tit">\s*(.*?)</div>', detail_html, re.DOTALL)
        if title_match:
            record.title = self._clean_book_title(self._html_to_text(title_match.group(1)))
        for item in re.findall(r"<li>\s*<strong>(.*?)</strong>\s*<div>(.*?)</div>\s*</li>", detail_html, re.DOTALL):
            label = self._html_to_text(item[0])
            value = self._html_to_text(item[1])
            self._fill_isbn_record_field(record, f"{label}: {value}")
        return record

    def _fill_isbn_record_field(self, record: IsbnRecord, text: str):
        text = " ".join(text.split())
        if text.startswith("저자"):
            record.author = re.sub(r"^(저자\s*:?\s*)+", "", text).strip()
        elif text.startswith("발행처"):
            record.publisher = re.sub(r"^발행처\s*:?\s*", "", text).strip()
        elif text.startswith("ISBN"):
            record.isbn = re.sub(r"^ISBN\s*:?\s*", "", text).strip()
        elif text.startswith("발행(예정)일"):
            record.date = re.sub(r"^발행\(예정\)일\s*:?\s*", "", text).strip()
        elif text.startswith("제본형태") or text.startswith("파일형식"):
            record.binding = re.sub(r"^(제본형태|파일형식)\s*:?\s*", "", text).strip()
        elif text.startswith("가격") or text.startswith("가격정보"):
            record.price = re.sub(r"^(가격|가격정보)\s*:?\s*", "", text).strip()
        elif text.startswith("키워드"):
            record.keywords = re.sub(r"^키워드\s*:?\s*", "", text).strip()

    def _html_to_text(self, value: str) -> str:
        value = re.sub(r"<script.*?</script>", "", value, flags=re.DOTALL | re.IGNORECASE)
        value = re.sub(r"<style.*?</style>", "", value, flags=re.DOTALL | re.IGNORECASE)
        value = re.sub(r"<[^>]+>", " ", value)
        return html.unescape(" ".join(value.split()))

    def _clean_book_title(self, title: str) -> str:
        title = re.sub(r"^\[[^\]]+\]\s*", "", title).strip()
        title = re.sub(r"^\d+\.\s*", "", title).strip()
        return title


class IsbnSearchThread(QThread):
    finished_ok = Signal(object, str)
    failed = Signal(str)

    def __init__(self, query: str, page: int, page_unit: int, parent=None):
        super().__init__(parent)
        self.query = query
        self.page = page
        self.page_unit = page_unit

    def run(self):
        try:
            result = TxtEpubConverter().search_isbn_page(
                self.query,
                page=self.page,
                page_unit=self.page_unit,
            )
            self.finished_ok.emit(result, self.query)
        except Exception as e:
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("안병헌 E북 메이커")
        self.resize(1350, 820)
        self.converter = TxtEpubConverter()
        self.marker_selection_line: Optional[int] = None
        self.subtitle_selection_line: Optional[int] = None
        self.extra_marker_selection_line: Optional[int] = None
        self.extra_subtitle_selection_line: Optional[int] = None
        self.isbn_records: List[IsbnRecord] = []
        self.isbn_current_page = 1
        self.isbn_total_pages = 0
        self.isbn_page_unit = 10
        self.isbn_search_thread: Optional[IsbnSearchThread] = None
        self.isbn_search_silent = False
        self.validation_issues: List[ValidationIssue] = []
        self.current_preview_chapter_index: Optional[int] = None
        self.current_preview_body_start: int = 0
        self.current_suspected_split_position: Optional[int] = None
        self.current_suspected_selection_range: Optional[tuple[int, int]] = None
        self.chapter_undo_stack: List[Tuple[List[Chapter], set[int]]] = []
        self.last_chapter_samples: List[str] = []
        self.last_extra_chapter_samples: List[str] = []
        self.refreshing_chapter_table = False
        self.refreshing_manual_delete_combo = False
        self.refreshing_preview_text = False
        self._build_ui()
        self.setAcceptDrops(True)

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        main_layout = QVBoxLayout(root)

        top_bar = QHBoxLayout()
        self.open_btn = QPushButton("TXT 열기")
        self.save_project_btn = QPushButton("작업 저장")
        self.load_project_btn = QPushButton("작업 가져오기")
        self.cover_btn = QPushButton("표지 선택")
        self.font_btn = QPushButton("폰트 선택")
        self.save_btn = QPushButton("단일 EPUB 생성")
        self.volume_save_btn = QPushButton("권별 EPUB 생성")

        for btn in [
            self.open_btn, self.save_project_btn, self.load_project_btn,
            self.cover_btn, self.font_btn, self.save_btn, self.volume_save_btn,
        ]:
            top_bar.addWidget(btn)
        top_bar.addStretch()
        main_layout.addLayout(top_bar)

        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter)

        self.left_tabs = QTabWidget()
        self.left_tabs.addTab(self._build_meta_tab(), "메타데이터")
        self.left_tabs.addTab(self._build_chapter_rule_tab(), "챕터 규칙")
        self.volume_tab_index = self.left_tabs.addTab(self._build_volume_tab(), "권 분할")

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.right_stack = QStackedWidget()
        right_layout.addWidget(self.right_stack)

        meta_right = QWidget()
        meta_right_layout = QVBoxLayout(meta_right)
        isbn_search_layout = QHBoxLayout()
        self.isbn_search_input = QLineEdit()
        self.isbn_search_input.setPlaceholderText("ISBN 서지 검색어")
        self.isbn_search_btn = QPushButton("ISBN 검색")
        self.isbn_site_btn = QPushButton("사이트 이동")
        isbn_search_layout.addWidget(self.isbn_search_input)
        isbn_search_layout.addWidget(self.isbn_search_btn)
        isbn_search_layout.addWidget(self.isbn_site_btn)
        meta_right_layout.addLayout(isbn_search_layout)

        self.isbn_result_table = QTableWidget(0, 5)
        self.isbn_result_table.setHorizontalHeaderLabels(["제목", "저자", "발행처", "ISBN", "발행일"])
        self.isbn_result_table.horizontalHeader().setStretchLastSection(True)
        meta_right_layout.addWidget(self.isbn_result_table)
        self.isbn_page_layout = QHBoxLayout()
        self.isbn_page_layout.addStretch()
        meta_right_layout.addLayout(self.isbn_page_layout)
        self.right_stack.addWidget(meta_right)

        chapter_right = QWidget()
        chapter_right_layout = QVBoxLayout(chapter_right)
        self.chapter_table = QTableWidget(0, 5)
        self.chapter_table.setHorizontalHeaderLabels(["수정", "제목", "부제", "줄 번호", "본문 글자수"])
        self.chapter_table.horizontalHeader().setStretchLastSection(True)
        self.chapter_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.chapter_table.setSelectionMode(QTableWidget.ExtendedSelection)
        chapter_header_layout = QHBoxLayout()
        chapter_header_layout.addWidget(QLabel("챕터 목록"))
        chapter_header_layout.addStretch()
        self.clear_chapters_btn = QPushButton("목록 클리어")
        chapter_header_layout.addWidget(self.clear_chapters_btn)
        chapter_right_layout.addLayout(chapter_header_layout)
        chapter_right_layout.addWidget(self.chapter_table)

        split_layout = QHBoxLayout()
        self.split_title_input = QLineEdit()
        self.split_title_input.setPlaceholderText("나눌 회차 제목: 예: 771화.")
        self.split_subtitle_input = QLineEdit()
        self.split_subtitle_input.setPlaceholderText("부제")
        self.set_split_subtitle_btn = QPushButton("선택=부제")
        self.split_chapter_btn = QPushButton("나누기")
        split_layout.addWidget(self.split_title_input)
        split_layout.addWidget(self.split_subtitle_input)
        split_layout.addWidget(self.set_split_subtitle_btn)
        split_layout.addWidget(self.split_chapter_btn)
        chapter_right_layout.addLayout(split_layout)

        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(False)
        preview_header_layout = QHBoxLayout()
        preview_header_layout.addWidget(QLabel("선택 챕터 본문"))
        self.body_find_input = QLineEdit()
        self.body_find_input.setPlaceholderText("본문 검색")
        self.body_find_prev_btn = QPushButton("이전")
        self.body_find_next_btn = QPushButton("다음")
        self.set_chapter_title_btn = QPushButton("제목")
        self.set_chapter_subtitle_btn = QPushButton("부제")
        self.convert_hanja_btn = QPushButton("한글")
        self.manual_split_btn = QPushButton("수동 나누기")
        self.manual_split_btn.setToolTip("단축키: F1")
        self.merge_chapter_up_btn = QPushButton("위로 합치기")
        self.manual_delete_lines_combo = QComboBox()
        self.manual_delete_lines_combo.addItems(["0줄", "1줄", "2줄", "3줄"])
        self.body_find_input.setFixedWidth(190)
        self.body_find_prev_btn.setFixedWidth(48)
        self.body_find_next_btn.setFixedWidth(48)
        self.set_chapter_title_btn.setFixedWidth(44)
        self.set_chapter_subtitle_btn.setFixedWidth(44)
        self.convert_hanja_btn.setFixedWidth(48)
        preview_header_layout.addStretch()
        preview_header_layout.addWidget(QLabel("찾기"))
        preview_header_layout.addWidget(self.body_find_input)
        preview_header_layout.addWidget(self.body_find_prev_btn)
        preview_header_layout.addWidget(self.body_find_next_btn)
        preview_header_layout.addWidget(self.set_chapter_title_btn)
        preview_header_layout.addWidget(self.set_chapter_subtitle_btn)
        preview_header_layout.addWidget(self.convert_hanja_btn)
        preview_header_layout.addWidget(self.manual_delete_lines_combo)
        preview_header_layout.addWidget(self.manual_split_btn)
        preview_header_layout.addWidget(self.merge_chapter_up_btn)
        chapter_right_layout.addLayout(preview_header_layout)
        chapter_right_layout.addWidget(self.preview_text, 1)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(95)
        chapter_right_layout.addWidget(QLabel("검증 / 로그"))
        chapter_right_layout.addWidget(self.log, 0)
        self.right_stack.addWidget(chapter_right)
        self.manual_split_shortcut = QShortcut(QKeySequence("F1"), self)
        self.manual_split_shortcut.activated.connect(self.manual_split_chapter_from_preview)

        volume_right = QWidget()
        volume_right_layout = QVBoxLayout(volume_right)
        self.volume_table = QTableWidget(0, 6)
        self.volume_table.setHorizontalHeaderLabels(["권", "시작 챕터", "끝 챕터", "포함 수", "예상 글자수", "예상 용량(KB)"])
        self.configure_volume_table_columns()
        volume_right_layout.addWidget(QLabel("권 분할 목록"))
        volume_right_layout.addWidget(self.volume_table)
        self.right_stack.addWidget(volume_right)

        splitter.addWidget(self.left_tabs)
        splitter.addWidget(right)
        splitter.setSizes([520, 830])

        self.open_btn.clicked.connect(self.open_txt)
        self.save_project_btn.clicked.connect(self.save_project_file)
        self.load_project_btn.clicked.connect(self.load_project_file)
        self.cover_btn.clicked.connect(self.select_cover)
        self.font_btn.clicked.connect(self.select_font)
        self.save_btn.clicked.connect(self.save_epub)
        self.volume_save_btn.clicked.connect(self.save_volume_epubs)
        self.isbn_search_btn.clicked.connect(self.search_isbn)
        self.isbn_site_btn.clicked.connect(self.open_isbn_site)
        self.isbn_result_table.itemDoubleClicked.connect(self.apply_selected_isbn)
        self.chapter_table.itemSelectionChanged.connect(self.update_preview_from_selection)
        self.chapter_table.itemChanged.connect(self.update_chapter_from_table_item)
        self.preview_text.textChanged.connect(self.update_chapter_body_from_preview)
        self.clear_chapters_btn.clicked.connect(self.clear_chapter_list)
        self.manual_split_btn.clicked.connect(self.manual_split_chapter_from_preview)
        self.merge_chapter_up_btn.clicked.connect(self.merge_selected_chapter_to_previous)
        self.set_chapter_title_btn.clicked.connect(self.set_chapter_title_from_preview_selection)
        self.set_chapter_subtitle_btn.clicked.connect(self.set_chapter_subtitle_from_preview_selection)
        self.convert_hanja_btn.clicked.connect(self.convert_selected_hanja_to_hangul)
        self.body_find_input.returnPressed.connect(lambda: self.find_in_preview(forward=True))
        self.body_find_prev_btn.clicked.connect(lambda: self.find_in_preview(forward=False))
        self.body_find_next_btn.clicked.connect(lambda: self.find_in_preview(forward=True))
        self.manual_delete_lines_combo.currentIndexChanged.connect(self.update_manual_delete_lines_for_current_chapter)
        self.set_split_subtitle_btn.clicked.connect(self.set_split_subtitle_from_selection)
        self.split_chapter_btn.clicked.connect(self.split_chapter_from_preview)
        self.chapters_per_volume_slider.valueChanged.connect(
            lambda value: self.sync_slider_to_number_input(
                self.chapters_per_volume_slider, self.chapters_per_volume_input, value, self.make_volume_preview_silent
            )
        )
        self.chapters_per_volume_input.textChanged.connect(
            lambda: self.sync_number_input_to_slider(self.chapters_per_volume_input, self.chapters_per_volume_slider, self.sync_volume_controls)
        )
        self.volume_after_volume_slider.valueChanged.connect(
            lambda value: self.sync_slider_to_number_input(
                self.volume_after_volume_slider, self.volume_after_volume_input, value, self.make_volume_preview_silent
            )
        )
        self.volume_after_volume_input.textChanged.connect(
            lambda: self.sync_number_input_to_slider(
                self.volume_after_volume_input, self.volume_after_volume_slider, self.make_volume_preview_silent, 0, False
            )
        )
        self.volume_after_count_slider.valueChanged.connect(
            lambda value: self.sync_slider_to_number_input(
                self.volume_after_count_slider, self.volume_after_count_input, value, self.make_volume_preview_silent
            )
        )
        self.volume_after_count_input.textChanged.connect(
            lambda: self.sync_number_input_to_slider(self.volume_after_count_input, self.volume_after_count_slider, self.make_volume_preview_silent)
        )
        self.volume_second_after_volume_slider.valueChanged.connect(
            lambda value: self.sync_slider_to_number_input(
                self.volume_second_after_volume_slider, self.volume_second_after_volume_input, value, self.make_volume_preview_silent
            )
        )
        self.volume_second_after_volume_input.textChanged.connect(
            lambda: self.sync_number_input_to_slider(
                self.volume_second_after_volume_input, self.volume_second_after_volume_slider, self.make_volume_preview_silent, 0, False
            )
        )
        self.volume_second_after_count_slider.valueChanged.connect(
            lambda value: self.sync_slider_to_number_input(
                self.volume_second_after_count_slider, self.volume_second_after_count_input, value, self.make_volume_preview_silent
            )
        )
        self.volume_second_after_count_input.textChanged.connect(
            lambda: self.sync_number_input_to_slider(
                self.volume_second_after_count_input, self.volume_second_after_count_slider, self.make_volume_preview_silent
            )
        )
        self.ignore_prev_line_check.stateChanged.connect(lambda: self.refresh_sample_display(False))
        self.ignore_prev_line_check.stateChanged.connect(self.rebuild_ignored_prev_lines)
        self.extra_ignore_prev_line_check.stateChanged.connect(lambda: self.refresh_sample_display(True))
        self.extra_ignore_prev_line_check.stateChanged.connect(self.rebuild_ignored_prev_lines)
        self.subtitle_sample_input.textChanged.connect(self.clear_subtitle_selection_if_empty)
        self.extra_subtitle_sample_input.textChanged.connect(self.clear_extra_subtitle_selection_if_empty)
        self.left_tabs.currentChanged.connect(self.right_stack.setCurrentIndex)
        self.left_tabs.currentChanged.connect(self.refresh_volume_preview_on_tab)
        self.update_font_label()

    def _build_meta_tab(self):
        box = QWidget()
        layout = QVBoxLayout(box)
        self.file_label = QLabel("TXT 파일: 없음")
        self.cover_label = QLabel("표지 파일: 없음")
        self.font_label = QLabel("폰트 파일: 없음")
        for label in [self.file_label, self.cover_label, self.font_label]:
            label.setWordWrap(False)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.file_label)
        layout.addWidget(self.cover_label)
        layout.addWidget(self.font_label)

        form = QFormLayout()
        self.title_input = QLineEdit()
        self.subtitle_input = QLineEdit()
        self.author_input = QLineEdit()
        self.translator_input = QLineEdit()
        self.publisher_input = QLineEdit()
        self.date_input = QLineEdit()
        self.ebook_publisher_input = QLineEdit("안병헌")
        self.isbn_input = QLineEdit()
        self.series_input = QLineEdit()
        self.volume_no_input = QLineEdit()
        self.total_volumes_input = QLineEdit()
        self.total_episodes_input = QLineEdit()
        self.episode_range_input = QLineEdit()
        self.completed_check = QCheckBox("완결")
        self.completed_check.setChecked(True)
        self.language_input = QLineEdit("ko")
        self.rights_input = QTextEdit()

        form.addRow("시리즈명", self.series_input)
        form.addRow("제목", self.title_input)
        form.addRow("부제", self.subtitle_input)
        form.addRow("지은이", self.author_input)
        form.addRow("옮긴이", self.translator_input)
        form.addRow("펴낸곳", self.publisher_input)
        form.addRow("발행일", self.date_input)
        form.addRow("전자책 발행인", self.ebook_publisher_input)
        form.addRow("권 번호", self.volume_no_input)
        form.addRow("총 권수", self.total_volumes_input)
        form.addRow("총 화수", self.total_episodes_input)
        form.addRow("수록 화수", self.episode_range_input)
        form.addRow("ISBN", self.isbn_input)
        form.addRow("판권/저작권 문구", self.rights_input)
        form.addRow("완결 여부", self.completed_check)
        form.addRow("언어", self.language_input)
        layout.addLayout(form)
        return box

    def _build_chapter_rule_tab(self):
        box = QWidget()
        layout = QVBoxLayout(box)
        self.marker_rule_btn = QPushButton("선택 규칙 적용")
        self.extract_sample_btn = QPushButton("챕터 후보 가져오기")
        self.analyze_sample_btn = QPushButton("샘플 분석")
        self.sample_text = QTextEdit()
        self.sample_text.setPlaceholderText("예:\n1화  서(序) ― 0회차\n2화  1회차의 첫날\n3화  첫 번째 선택")
        self.sample_text.setFixedHeight(self.sample_text.fontMetrics().lineSpacing() * 6 + 18)
        self.marker_sample_input = QLineEdit()
        self.marker_sample_input.setPlaceholderText("샘플에서 '1화' 같은 분류기호를 선택 후 버튼 클릭")
        self.subtitle_prefix_input = QLineEdit()
        self.subtitle_prefix_input.setPlaceholderText("부제 앞에 붙일 단어: 예: 외전 -")
        self.subtitle_sample_input = QLineEdit()
        self.subtitle_sample_input.setPlaceholderText("샘플에서 부제 부분을 선택 후 버튼 클릭")
        self.ignore_prev_line_check = QCheckBox("윗줄 무시")
        self.extract_extra_sample_btn = QPushButton("추가 챕터 후보 가져오기")
        self.analyze_extra_sample_btn = QPushButton("추가 샘플 분석")
        self.extra_marker_sample_input = QLineEdit()
        self.extra_marker_sample_input.setPlaceholderText("정규 마지막 이후 추가편 분류기호 선택: 예: 2차 외전")
        self.extra_subtitle_prefix_input = QLineEdit()
        self.extra_subtitle_prefix_input.setPlaceholderText("추가 부제 앞에 붙일 단어: 예: 외전 -")
        self.extra_subtitle_sample_input = QLineEdit()
        self.extra_subtitle_sample_input.setPlaceholderText("추가편 부제 부분을 선택 후 버튼 클릭")
        self.extra_ignore_prev_line_check = QCheckBox("추가 윗줄 무시")
        self.set_marker_btn = QPushButton("선택=분류기호")
        self.set_subtitle_btn = QPushButton("선택=부제")
        self.set_extra_marker_btn = QPushButton("선택=추가 분류기호")
        self.set_extra_subtitle_btn = QPushButton("선택=추가 부제")
        self.regex_input = QLineEdit()
        self.regex_input.setReadOnly(True)
        self.regex_input.setPlaceholderText("선택 규칙 적용 시 자동 생성")

        sample_btn_layout = QHBoxLayout()
        sample_btn_layout.addWidget(self.extract_sample_btn)
        sample_btn_layout.addWidget(self.analyze_sample_btn)
        layout.addLayout(sample_btn_layout)
        layout.addWidget(self.sample_text)
        ignore_layout = QHBoxLayout()
        ignore_layout.addWidget(self.ignore_prev_line_check)
        layout.addLayout(ignore_layout)
        marker_layout = QHBoxLayout()
        marker_layout.addWidget(self.marker_sample_input)
        marker_layout.addWidget(self.set_marker_btn)
        layout.addLayout(marker_layout)
        subtitle_layout = QHBoxLayout()
        subtitle_layout.addWidget(self.subtitle_prefix_input)
        subtitle_layout.addWidget(self.subtitle_sample_input)
        subtitle_layout.addWidget(self.set_subtitle_btn)
        layout.addLayout(subtitle_layout)
        extra_sample_btn_layout = QHBoxLayout()
        extra_sample_btn_layout.addWidget(self.extract_extra_sample_btn)
        extra_sample_btn_layout.addWidget(self.analyze_extra_sample_btn)
        layout.addLayout(extra_sample_btn_layout)
        self.extra_sample_text = QTextEdit()
        self.extra_sample_text.setPlaceholderText("추가 챕터 후보")
        self.extra_sample_text.setFixedHeight(self.extra_sample_text.fontMetrics().lineSpacing() * 6 + 18)
        layout.addWidget(self.extra_sample_text)
        extra_ignore_layout = QHBoxLayout()
        extra_ignore_layout.addWidget(self.extra_ignore_prev_line_check)
        layout.addLayout(extra_ignore_layout)
        extra_marker_layout = QHBoxLayout()
        extra_marker_layout.addWidget(self.extra_marker_sample_input)
        extra_marker_layout.addWidget(self.set_extra_marker_btn)
        layout.addLayout(extra_marker_layout)
        extra_subtitle_layout = QHBoxLayout()
        extra_subtitle_layout.addWidget(self.extra_subtitle_prefix_input)
        extra_subtitle_layout.addWidget(self.extra_subtitle_sample_input)
        extra_subtitle_layout.addWidget(self.set_extra_subtitle_btn)
        layout.addLayout(extra_subtitle_layout)
        layout.addWidget(QLabel("생성된 규칙"))
        layout.addWidget(self.regex_input)
        layout.addWidget(self.marker_rule_btn)
        self.validation_table = QTableWidget(0, 2)
        self.validation_table.setHorizontalHeaderLabels(["누락 번호", "위치"])
        self.validation_table.horizontalHeader().setStretchLastSection(True)
        self.validation_table.setMaximumHeight(180)
        layout.addWidget(QLabel("누락 챕터"))
        layout.addWidget(self.validation_table)
        self.undo_split_btn = QPushButton("되돌리기")
        layout.addWidget(self.undo_split_btn)
        layout.addStretch()

        self.extract_sample_btn.clicked.connect(self.extract_chapter_samples)
        self.analyze_sample_btn.clicked.connect(lambda: self.analyze_sample_text(False))
        self.extract_extra_sample_btn.clicked.connect(self.extract_extra_chapter_samples)
        self.analyze_extra_sample_btn.clicked.connect(lambda: self.analyze_sample_text(True))
        self.marker_rule_btn.clicked.connect(self.detect_by_marker_rule)
        self.set_marker_btn.clicked.connect(self.set_marker_from_selection)
        self.set_subtitle_btn.clicked.connect(self.set_subtitle_from_selection)
        self.set_extra_marker_btn.clicked.connect(self.set_extra_marker_from_selection)
        self.set_extra_subtitle_btn.clicked.connect(self.set_extra_subtitle_from_selection)
        self.validation_table.itemClicked.connect(self.select_validation_issue)
        self.undo_split_btn.clicked.connect(self.undo_chapter_split)
        return box

    def configure_volume_table_columns(self):
        header = self.volume_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        self.volume_table.setColumnWidth(0, 44)
        self.volume_table.setColumnWidth(3, 64)
        self.volume_table.setColumnWidth(4, 92)
        self.volume_table.setColumnWidth(5, 108)

    def make_number_input(self, minimum: int, maximum: int, value: int) -> QLineEdit:
        line_edit = QLineEdit(str(value))
        line_edit.setValidator(QIntValidator(minimum, maximum, self))
        line_edit.setFixedWidth(64)
        line_edit.setAlignment(Qt.AlignRight)
        return line_edit

    def slider_number_layout(self, slider: QSlider, number_input: QLineEdit) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(slider)
        row.addWidget(number_input)
        return row

    def _build_volume_tab(self):
        box = QWidget()
        layout = QVBoxLayout(box)
        form = QFormLayout()
        self.chapters_per_volume_slider = QSlider(Qt.Horizontal)
        self.chapters_per_volume_slider.setRange(80, 120)
        self.chapters_per_volume_slider.setValue(100)
        self.chapters_per_volume_input = self.make_number_input(1, 10000, 100)
        chapters_per_volume_layout = self.slider_number_layout(
            self.chapters_per_volume_slider, self.chapters_per_volume_input
        )

        self.volume_after_volume_slider = QSlider(Qt.Horizontal)
        self.volume_after_volume_slider.setRange(0, 20)
        self.volume_after_volume_slider.setValue(0)
        self.volume_after_volume_input = self.make_number_input(0, 10000, 0)
        after_volume_layout = self.slider_number_layout(
            self.volume_after_volume_slider, self.volume_after_volume_input
        )

        self.volume_after_count_slider = QSlider(Qt.Horizontal)
        self.volume_after_count_slider.setRange(80, 120)
        self.volume_after_count_slider.setValue(self.number_input_value(self.chapters_per_volume_input, 100))
        self.volume_after_count_input = self.make_number_input(1, 10000, self.volume_after_count_slider.value())
        after_count_layout = self.slider_number_layout(
            self.volume_after_count_slider, self.volume_after_count_input
        )

        self.volume_second_after_volume_slider = QSlider(Qt.Horizontal)
        self.volume_second_after_volume_slider.setRange(0, 20)
        self.volume_second_after_volume_slider.setValue(0)
        self.volume_second_after_volume_input = self.make_number_input(0, 10000, 0)
        second_after_volume_layout = self.slider_number_layout(
            self.volume_second_after_volume_slider, self.volume_second_after_volume_input
        )

        self.volume_second_after_count_slider = QSlider(Qt.Horizontal)
        self.volume_second_after_count_slider.setRange(80, 120)
        self.volume_second_after_count_slider.setValue(self.volume_after_count_slider.value())
        self.volume_second_after_count_input = self.make_number_input(1, 10000, self.volume_second_after_count_slider.value())
        second_after_count_layout = self.slider_number_layout(
            self.volume_second_after_count_slider, self.volume_second_after_count_input
        )

        form.addRow("권당 챕터 수", chapters_per_volume_layout)
        form.addRow("몇권부터", after_volume_layout)
        form.addRow("이후 권당 챕터 수", after_count_layout)
        form.addRow("2차 몇권부터", second_after_volume_layout)
        form.addRow("2차 이후 권당 챕터 수", second_after_count_layout)
        layout.addLayout(form)
        layout.addStretch()
        return box

    def open_txt(self):
        path, _ = QFileDialog.getOpenFileName(self, "TXT 파일 선택", "", "Text Files (*.txt);;All Files (*)")
        if not path:
            return
        self.load_txt_file(path)

    def load_txt_file(self, path: str):
        try:
            self.reset_all(log=False)
            encoding = self.converter.load_txt(path)
            self.set_path_label(self.file_label, "TXT 파일", path)
            title, author = self.converter.parse_title_author_from_filename(path)
            self.title_input.setText(title)
            self.isbn_search_input.setText(title)
            if author:
                self.author_input.setText(author)
            cover_path = self.converter.find_cover_for_txt(path)
            self.converter.cover_path = cover_path or None
            if cover_path:
                self.set_path_label(self.cover_label, "표지 파일", cover_path)
                self.log.append(f"표지 자동 선택: {cover_path}")
            else:
                self.cover_label.setText("표지 파일: 없음")
                self.cover_label.setToolTip("")
            self.update_font_label()
            self.log.append(f"TXT 로드 완료: {encoding}")
            self.auto_search_isbn_after_load()
            self.auto_detect_chapters_after_load()
        except Exception as e:
            QMessageBox.critical(self, "오류", str(e))

    def auto_search_isbn_after_load(self):
        try:
            query = self.isbn_search_input.text().strip() or self.title_input.text().strip()
            if not query:
                return
            self.search_isbn_page(1, silent=True)
        except Exception as e:
            self.log.append(f"ISBN 자동 검색 실패: {e}")

    def auto_detect_chapters_after_load(self):
        if not self._extract_chapter_samples(silent=True):
            return
        self.detect_by_marker_rule(silent=True)

        if self._extract_extra_chapter_samples(silent=True):
            self.detect_by_marker_rule(silent=True)

    def dragEnterEvent(self, event):
        if any(url.toLocalFile().lower().endswith(".txt") for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".txt"):
                self.load_txt_file(path)
                event.acceptProposedAction()
                return
        event.ignore()

    def save_project_file(self):
        try:
            if not self.converter.txt_path:
                raise ValueError("먼저 TXT 파일을 불러오세요.")
            default_name = os.path.splitext(os.path.basename(self.converter.txt_path))[0] + ".abhwork"
            default_path = os.path.join(os.path.dirname(self.converter.txt_path), default_name)
            path, _ = QFileDialog.getSaveFileName(
                self, "작업 저장", default_path, "EPUB 작업 파일 (*.abhwork);;JSON Files (*.json);;All Files (*)"
            )
            if not path:
                return
            if not os.path.splitext(path)[1]:
                path += ".abhwork"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.build_project_state(), f, ensure_ascii=False, indent=2)
            self.log.append(f"작업 저장 완료: {path}")
        except Exception as e:
            QMessageBox.critical(self, "작업 저장 오류", str(e))

    def load_project_file(self):
        try:
            path, _ = QFileDialog.getOpenFileName(
                self, "작업 가져오기", "", "EPUB 작업 파일 (*.abhwork *.json);;All Files (*)"
            )
            if not path:
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.restore_project_state(data)
            self.log.append(f"작업 가져오기 완료: {path}")
        except Exception as e:
            QMessageBox.critical(self, "작업 가져오기 오류", str(e))

    def build_project_state(self) -> dict:
        return {
            "format": "abh_epub_maker_project",
            "version": 1,
            "paths": {
                "txt": self.converter.txt_path,
                "cover": self.converter.cover_path,
                "font": self.converter.font_path,
            },
            "meta": self.collect_meta(),
            "chapter_rule": {
                "marker_selection_line": self.marker_selection_line,
                "subtitle_selection_line": self.subtitle_selection_line,
                "extra_marker_selection_line": self.extra_marker_selection_line,
                "extra_subtitle_selection_line": self.extra_subtitle_selection_line,
                "marker_sample": self.marker_sample_input.text(),
                "subtitle_prefix": self.subtitle_prefix_input.text(),
                "subtitle_sample": self.subtitle_sample_input.text(),
                "extra_marker_sample": self.extra_marker_sample_input.text(),
                "extra_subtitle_prefix": self.extra_subtitle_prefix_input.text(),
                "extra_subtitle_sample": self.extra_subtitle_sample_input.text(),
                "regex": self.regex_input.text(),
                "ignore_prev_line": self.ignore_prev_line_check.isChecked(),
                "extra_ignore_prev_line": self.extra_ignore_prev_line_check.isChecked(),
                "sample_text": self.sample_text.toPlainText(),
                "extra_sample_text": self.extra_sample_text.toPlainText(),
                "last_chapter_samples": self.last_chapter_samples,
                "last_extra_chapter_samples": self.last_extra_chapter_samples,
            },
            "chapters": [vars(ch) for ch in self.converter.chapters],
            "ignored_body_line_indexes": sorted(self.converter.ignored_body_line_indexes),
            "volume": {
                "chapters_per_volume": self.chapters_per_volume_input.text(),
                "after_volume": self.volume_after_volume_input.text(),
                "after_count": self.volume_after_count_input.text(),
                "second_after_volume": self.volume_second_after_volume_input.text(),
                "second_after_count": self.volume_second_after_count_input.text(),
            },
            "isbn": {
                "query": self.isbn_search_input.text(),
                "current_page": self.isbn_current_page,
                "total_pages": self.isbn_total_pages,
                "records": [vars(record) for record in self.isbn_records],
            },
        }

    def restore_project_state(self, data: dict):
        if data.get("format") != "abh_epub_maker_project":
            raise ValueError("안병헌 E북 메이커 작업 파일이 아닙니다.")

        paths = data.get("paths", {})
        txt_path = paths.get("txt") or ""
        if not txt_path or not os.path.exists(txt_path):
            raise ValueError(f"원본 TXT 파일을 찾지 못했습니다.\n{txt_path}")

        self.reset_all(log=False)
        encoding = self.converter.load_txt(txt_path)
        self.set_path_label(self.file_label, "TXT 파일", txt_path)

        cover_path = paths.get("cover") or ""
        self.converter.cover_path = cover_path or None
        if cover_path:
            self.set_path_label(self.cover_label, "표지 파일", cover_path)

        self.converter.font_path = paths.get("font") or None
        self.update_font_label()

        self.apply_project_meta(data.get("meta", {}))
        self.apply_project_chapter_rule(data.get("chapter_rule", {}))
        self.apply_project_isbn(data.get("isbn", {}))

        self.converter.chapters = self.converter._finalize_chapter_ranges(
            [Chapter(**chapter) for chapter in data.get("chapters", [])]
        )
        self.converter.ignored_body_line_indexes = set(data.get("ignored_body_line_indexes", []))
        self.converter.volume_ranges = []
        self.chapter_undo_stack = []
        self.refresh_chapter_table(self.converter.chapters)
        if self.converter.chapters:
            self.select_chapter_row(0)
        self.apply_project_volume(data.get("volume", {}))
        self.log.append(f"TXT 로드 완료: {encoding}")

    def apply_project_meta(self, meta: dict):
        fields = {
            "title": self.title_input,
            "subtitle": self.subtitle_input,
            "creator": self.author_input,
            "translator": self.translator_input,
            "publisher": self.publisher_input,
            "date": self.date_input,
            "ebook_publisher": self.ebook_publisher_input,
            "isbn": self.isbn_input,
            "series": self.series_input,
            "volume_no": self.volume_no_input,
            "total_volumes": self.total_volumes_input,
            "total_episodes": self.total_episodes_input,
            "episode_range": self.episode_range_input,
            "language": self.language_input,
        }
        for key, widget in fields.items():
            widget.setText(str(meta.get(key, "")))
        self.completed_check.setChecked(bool(meta.get("completed", True)))
        self.rights_input.setPlainText(str(meta.get("rights", "")))

    def apply_project_chapter_rule(self, rule: dict):
        self.marker_selection_line = rule.get("marker_selection_line")
        self.subtitle_selection_line = rule.get("subtitle_selection_line")
        self.extra_marker_selection_line = rule.get("extra_marker_selection_line")
        self.extra_subtitle_selection_line = rule.get("extra_subtitle_selection_line")
        self.marker_sample_input.setText(rule.get("marker_sample", ""))
        self.subtitle_prefix_input.setText(rule.get("subtitle_prefix", ""))
        self.subtitle_sample_input.setText(rule.get("subtitle_sample", ""))
        self.extra_marker_sample_input.setText(rule.get("extra_marker_sample", ""))
        self.extra_subtitle_prefix_input.setText(rule.get("extra_subtitle_prefix", ""))
        self.extra_subtitle_sample_input.setText(rule.get("extra_subtitle_sample", ""))
        self.regex_input.setText(rule.get("regex", ""))
        self.ignore_prev_line_check.setChecked(bool(rule.get("ignore_prev_line", False)))
        self.extra_ignore_prev_line_check.setChecked(bool(rule.get("extra_ignore_prev_line", False)))
        self.sample_text.setPlainText(rule.get("sample_text", ""))
        self.extra_sample_text.setPlainText(rule.get("extra_sample_text", ""))
        self.last_chapter_samples = list(rule.get("last_chapter_samples", []))
        self.last_extra_chapter_samples = list(rule.get("last_extra_chapter_samples", []))

    def apply_project_volume(self, volume: dict):
        self.chapters_per_volume_input.setText(str(volume.get("chapters_per_volume", "100")))
        self.volume_after_volume_input.setText(str(volume.get("after_volume", "0")))
        self.volume_after_count_input.setText(str(volume.get("after_count", "100")))
        self.volume_second_after_volume_input.setText(str(volume.get("second_after_volume", "0")))
        self.volume_second_after_count_input.setText(str(volume.get("second_after_count", "100")))
        self.sync_volume_controls()
        self.sync_number_input_to_slider(
            self.volume_after_volume_input, self.volume_after_volume_slider, self.make_volume_preview_silent, 0, False
        )
        self.sync_number_input_to_slider(
            self.volume_after_count_input, self.volume_after_count_slider, self.make_volume_preview_silent
        )
        self.sync_number_input_to_slider(
            self.volume_second_after_volume_input, self.volume_second_after_volume_slider, self.make_volume_preview_silent, 0, False
        )
        self.sync_number_input_to_slider(
            self.volume_second_after_count_input, self.volume_second_after_count_slider, self.make_volume_preview_silent
        )

    def apply_project_isbn(self, isbn: dict):
        self.isbn_search_input.setText(isbn.get("query", ""))
        self.isbn_current_page = int(isbn.get("current_page", 1) or 1)
        self.isbn_total_pages = int(isbn.get("total_pages", 0) or 0)
        self.isbn_records = [IsbnRecord(**record) for record in isbn.get("records", [])]
        self.refresh_isbn_result_table()
        self.refresh_isbn_page_buttons()

    def extract_chapter_samples(self):
        self._extract_chapter_samples(silent=False)

    def _extract_chapter_samples(self, silent: bool = False) -> bool:
        try:
            if not self.converter.text:
                raise ValueError("TXT 파일을 먼저 불러오세요.")
            samples = self.converter.find_chapter_sample_lines(3)
            if not samples:
                raise ValueError("1화부터 5화까지의 후보 줄을 찾지 못했습니다.")
            self.last_chapter_samples = samples
            self.update_ignored_prev_lines(extra=False)
            self.sample_text.setPlainText("\n\n".join(samples))
            self.apply_sample_guess(samples, extra=False)
            self.log.append(f"챕터 후보 추출: {len(samples)}개")
            return True
        except Exception as e:
            if silent:
                self.log.append(f"챕터 후보 자동 처리 실패: {e}")
            else:
                QMessageBox.critical(self, "오류", str(e))
            return False

    def extract_extra_chapter_samples(self):
        self._extract_extra_chapter_samples(silent=False)

    def _extract_extra_chapter_samples(self, silent: bool = False) -> bool:
        try:
            if not self.converter.text:
                raise ValueError("TXT 파일을 먼저 불러오세요.")
            regular_regex = self.converter.build_regex_from_marker_sample(
                self.marker_sample_input.text(),
                self.subtitle_sample_input.text(),
            )
            samples = self.converter.find_extra_chapter_sample_lines(
                regular_regex, 3
            )
            if not samples:
                raise ValueError("정규 마지막 챕터 이후 추가 챕터 후보를 찾지 못했습니다.")

            self.last_extra_chapter_samples = samples
            self.update_ignored_prev_lines(extra=True)
            self.extra_sample_text.setPlainText("\n\n".join(samples))
            self.apply_sample_guess(samples, extra=True)
            self.regex_input.setText(regular_regex)
            self.log.append(f"추가 챕터 후보 추출: {len(samples)}개")
            return True
        except Exception as e:
            if silent:
                self.log.append(f"추가 챕터 후보 자동 처리 실패: {e}")
            else:
                QMessageBox.critical(self, "오류", str(e))
            return False

    def set_marker_from_selection(self):
        selected, position = self._selection_from_editor(self.sample_text)
        if not selected:
            QMessageBox.information(self, "선택 없음", "샘플 텍스트에서 분류기호를 선택하세요.")
            return
        self.marker_sample_input.setText(selected)
        self.marker_selection_line = position
        self.log.append(f"분류기호 샘플 지정: {selected}")

    def set_subtitle_from_selection(self):
        selected, position = self._selection_from_editor(self.sample_text)
        if not selected:
            QMessageBox.information(self, "선택 없음", "샘플 텍스트에서 부제 부분을 선택하세요.")
            return
        self.subtitle_sample_input.setText(selected)
        self.subtitle_selection_line = position
        self.log.append(f"부제 샘플 지정: {selected}")

    def set_extra_marker_from_selection(self):
        selected, position = self._selection_from_editor(self.extra_sample_text)
        if not selected:
            QMessageBox.information(self, "선택 없음", "샘플 텍스트에서 추가/외전 분류기호를 선택하세요.")
            return
        self.extra_marker_sample_input.setText(selected)
        self.extra_marker_selection_line = position
        self.log.append(f"추가/외전 분류기호 샘플 지정: {selected}")

    def set_extra_subtitle_from_selection(self):
        selected, position = self._selection_from_editor(self.extra_sample_text)
        if not selected:
            QMessageBox.information(self, "선택 없음", "샘플 텍스트에서 추가/외전 부제 부분을 선택하세요.")
            return
        self.extra_subtitle_sample_input.setText(selected)
        self.extra_subtitle_selection_line = position
        self.log.append(f"추가/외전 부제 샘플 지정: {selected}")

    def clear_subtitle_selection_if_empty(self, text: str):
        if not text.strip():
            self.subtitle_selection_line = None

    def clear_extra_subtitle_selection_if_empty(self, text: str):
        if not text.strip():
            self.extra_subtitle_selection_line = None

    def apply_sample_guess(self, samples: List[str], extra: bool):
        ignore_prev = self.extra_ignore_prev_line_check.isChecked() if extra else self.ignore_prev_line_check.isChecked()
        guess = self.converter.guess_rule_from_sample_lines(
            samples,
            ignore_prev_line=ignore_prev,
        )
        if not guess:
            return

        sample_text = self.extra_sample_text if extra else self.sample_text
        marker_input = self.extra_marker_sample_input if extra else self.marker_sample_input
        subtitle_input = self.extra_subtitle_sample_input if extra else self.subtitle_sample_input

        marker_input.setText(guess.marker)
        subtitle_input.setText(guess.subtitle)

        marker_pos = sample_text.toPlainText().find(guess.marker)
        subtitle_pos = sample_text.toPlainText().find(guess.subtitle, max(marker_pos, 0))

        if extra:
            self.extra_marker_selection_line = marker_pos if marker_pos >= 0 else None
            self.extra_subtitle_selection_line = subtitle_pos if subtitle_pos >= 0 else None
        else:
            self.marker_selection_line = marker_pos if marker_pos >= 0 else None
            self.subtitle_selection_line = subtitle_pos if subtitle_pos >= 0 else None

        label = "추가 챕터" if extra else "챕터"
        source = "아랫줄" if guess.subtitle_from_next_line else "첫 줄"
        self.log.append(f"{label} 규칙 자동 추정: 분류기호 [{guess.marker}], 부제 [{guess.subtitle}] / {source}")

    def sample_blocks_from_editor(self, extra: bool) -> List[str]:
        editor = self.extra_sample_text if extra else self.sample_text
        text = editor.toPlainText().strip()
        if not text:
            return []
        blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
        return blocks or [text]

    def analyze_sample_text(self, extra: bool = False, silent: bool = False) -> bool:
        samples = self.sample_blocks_from_editor(extra)
        if not samples:
            if not silent:
                QMessageBox.information(self, "샘플 없음", "샘플창에 챕터 후보를 몇 개 입력하세요.")
            return False
        try:
            self.apply_sample_guess(samples, extra=extra)
            return True
        except Exception as e:
            if not silent:
                QMessageBox.critical(self, "샘플 분석 오류", str(e))
            return False

    def reapply_sample_guess(self, extra: bool):
        samples = self.sample_blocks_from_editor(extra)
        if samples:
            self.apply_sample_guess(samples, extra=extra)

    def refresh_sample_display(self, extra: bool):
        raw_samples = self.last_extra_chapter_samples if extra else self.last_chapter_samples
        if not raw_samples:
            self.reapply_sample_guess(extra)
            return
        self.update_ignored_prev_lines(extra)
        samples = list(raw_samples)
        editor = self.extra_sample_text if extra else self.sample_text
        editor.setPlainText("\n\n".join(samples))
        self.apply_sample_guess(samples, extra=extra)

    def update_ignored_prev_lines(self, extra: bool = False):
        ignore_prev = self.extra_ignore_prev_line_check.isChecked() if extra else self.ignore_prev_line_check.isChecked()
        if not ignore_prev:
            return
        raw_samples = self.last_extra_chapter_samples if extra else self.last_chapter_samples
        patterns = self.infer_prev_ignore_patterns(raw_samples)
        for sample in raw_samples:
            prev_line, marker_line = self.sample_prev_and_marker_lines(sample)
            if not prev_line or not marker_line:
                continue
            marker_index = self._find_line_index_by_text(marker_line)
            prev_index = self.previous_content_line_index(marker_index) if marker_index is not None else None
            if prev_index is None:
                continue
            if self.prev_line_matches_ignore_patterns(self.converter.lines[prev_index].strip(), patterns):
                self.converter.ignored_body_line_indexes.add(prev_index)

    def rebuild_ignored_prev_lines(self):
        self.converter.ignored_body_line_indexes = set()
        if not (self.ignore_prev_line_check.isChecked() or self.extra_ignore_prev_line_check.isChecked()):
            self.update_preview_from_selection()
            return
        patterns = self.active_prev_ignore_patterns()
        for chapter in self.converter.chapters:
            prev_index = self.previous_content_line_index(chapter.line_index)
            if prev_index is not None and self.prev_line_matches_ignore_patterns(self.converter.lines[prev_index].strip(), patterns):
                self.converter.ignored_body_line_indexes.add(prev_index)
        self.update_preview_from_selection()

    def previous_content_line_index(self, line_index: int) -> Optional[int]:
        if line_index is None:
            return None
        for idx in range(line_index - 1, -1, -1):
            if self.converter.lines[idx].strip():
                return idx
        return None

    def active_prev_ignore_patterns(self) -> List[re.Pattern]:
        patterns: List[re.Pattern] = []
        if self.ignore_prev_line_check.isChecked():
            patterns.extend(self.infer_prev_ignore_patterns(self.last_chapter_samples))
        if self.extra_ignore_prev_line_check.isChecked():
            patterns.extend(self.infer_prev_ignore_patterns(self.last_extra_chapter_samples))
        return patterns

    def infer_prev_ignore_patterns(self, samples: List[str]) -> List[re.Pattern]:
        prev_lines = []
        for sample in samples:
            prev_line, _ = self.sample_prev_and_marker_lines(sample)
            if prev_line:
                prev_lines.append(prev_line)
        pattern = self.numbered_line_pattern(prev_lines)
        return [pattern] if pattern else []

    def sample_prev_and_marker_lines(self, sample: str) -> tuple[str, str]:
        lines = [line.strip() for line in sample.splitlines() if line.strip()]
        if len(lines) < 2:
            return "", ""
        marker_re = re.compile(r"(?:제\s*)?\d{1,5}\s*(?:화|장|회|차)\s*[\.\)\]]?", re.IGNORECASE)
        for idx, line in enumerate(lines):
            if marker_re.search(line):
                prev_line = lines[idx - 1] if idx > 0 else ""
                return prev_line, line
        return lines[0], lines[1]

    def numbered_line_pattern(self, lines: List[str]) -> Optional[re.Pattern]:
        clean_lines = [line.strip() for line in lines if line and line.strip()]
        if not clean_lines:
            return None
        first = clean_lines[0]
        if not re.search(r"\d+", first):
            if all(line == first for line in clean_lines):
                return re.compile(rf"^{re.escape(first)}$")
            return None

        pieces = re.split(r"(\d+)", first)
        regex_parts = []
        for piece in pieces:
            if not piece:
                continue
            if piece.isdigit():
                regex_parts.append(r"\d{1,5}")
            else:
                escaped = re.escape(piece)
                escaped = re.sub(r"(?:\\\s)+", r"\\s*", escaped)
                regex_parts.append(escaped)
        pattern = re.compile("^" + "".join(regex_parts) + "$")
        if all(pattern.match(line) for line in clean_lines):
            return pattern
        return None

    def prev_line_matches_ignore_patterns(self, line: str, patterns: List[re.Pattern]) -> bool:
        return bool(patterns) and any(pattern.match(line.strip()) for pattern in patterns)

    def _find_line_index_by_text(self, text: str) -> Optional[int]:
        for idx, line in enumerate(self.converter.lines):
            if line.strip() == text.strip():
                return idx
        return None

    def filtered_sample_blocks(self, samples: List[str], extra: bool) -> List[str]:
        filtered = []
        for sample in samples:
            lines = [line for line in sample.splitlines() if line.strip()]
            filtered.append("\n".join(lines))
        return filtered

    def detect_by_marker_rule(self, silent: bool = False):
        try:
            if not self.marker_sample_input.text().strip() and self.sample_text.toPlainText().strip():
                self.analyze_sample_text(False, silent=True)
            if (
                not self.extra_marker_sample_input.text().strip()
                and self.extra_sample_text.toPlainText().strip()
            ):
                self.analyze_sample_text(True, silent=True)
            regular_subtitle_enabled = bool(self.subtitle_sample_input.text().strip())
            extra_subtitle_enabled = bool(self.extra_subtitle_sample_input.text().strip())
            if not regular_subtitle_enabled:
                self.subtitle_selection_line = None
            if not extra_subtitle_enabled:
                self.extra_subtitle_selection_line = None
            regex = self.converter.build_regex_from_marker_sample(
                self.marker_sample_input.text(),
                self.subtitle_sample_input.text(),
            )
            extra_regex = ""
            if self.extra_marker_sample_input.text().strip():
                extra_regex = self.converter.build_regex_from_marker_sample(
                    self.extra_marker_sample_input.text(),
                    self.extra_subtitle_sample_input.text(),
                    allow_prefix=True,
                )
            self.regex_input.setText(regex + ("\n" + extra_regex if extra_regex else ""))
            chapters = self.converter.detect_chapters_by_marker_rules(
                regex, extra_regex,
                regular_subtitle_from_next_line=regular_subtitle_enabled and self._uses_next_line_subtitle(False),
                extra_subtitle_from_next_line=extra_subtitle_enabled and self._uses_next_line_subtitle(True),
                regular_subtitle_prefix=self.subtitle_prefix_input.text() if regular_subtitle_enabled else "",
                extra_subtitle_prefix=self.extra_subtitle_prefix_input.text() if extra_subtitle_enabled else "",
            )
            self.rebuild_ignored_prev_lines()
            self.refresh_chapter_table(chapters)
            self.log.append("선택 기반 규칙 적용")
            self.show_validation()
        except Exception as e:
            if silent:
                self.log.append(f"선택 규칙 자동 적용 실패: {e}")
            else:
                QMessageBox.critical(self, "오류", str(e))

    def select_cover(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "표지 파일 선택", "", "Images (*.jpg *.jpeg *.png *.webp);;All Files (*)"
        )
        if not path:
            return
        self.converter.cover_path = path
        self.set_path_label(self.cover_label, "표지 파일", path)

    def select_font(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "폰트 파일 선택", "", "Fonts (*.ttf *.otf *.woff *.woff2);;All Files (*)"
        )
        if not path:
            return
        self.converter.font_path = path
        self.update_font_label()

    def update_font_label(self):
        font_path = self.converter._font_path()
        if font_path:
            self.set_path_label(self.font_label, "폰트 파일", font_path)
        else:
            self.font_label.setText("폰트 파일: 없음")
            self.font_label.setToolTip("")

    def set_path_label(self, label: QLabel, prefix: str, path: str):
        label.setText(f"{prefix}: {os.path.basename(path)}")
        label.setToolTip(path)

    def reset_all(self, log: bool = True):
        self.converter = TxtEpubConverter()
        self.marker_selection_line = None
        self.subtitle_selection_line = None
        self.extra_marker_selection_line = None
        self.extra_subtitle_selection_line = None
        self.isbn_records = []
        self.isbn_current_page = 1
        self.isbn_total_pages = 0
        self.validation_issues = []
        self.current_preview_chapter_index = None
        self.current_preview_body_start = 0
        self.current_suspected_split_position = None
        self.current_suspected_selection_range = None
        self.chapter_undo_stack = []
        self.last_chapter_samples = []
        self.last_extra_chapter_samples = []

        self.file_label.setText("TXT 파일: 없음")
        self.file_label.setToolTip("")
        self.cover_label.setText("표지 파일: 없음")
        self.cover_label.setToolTip("")
        self.font_label.setText("폰트 파일: 없음")
        self.font_label.setToolTip("")

        for line_edit in [
            self.title_input,
            self.subtitle_input,
            self.author_input,
            self.translator_input,
            self.publisher_input,
            self.date_input,
            self.isbn_input,
            self.series_input,
            self.volume_no_input,
            self.total_volumes_input,
            self.total_episodes_input,
            self.episode_range_input,
            self.isbn_search_input,
            self.marker_sample_input,
            self.subtitle_prefix_input,
            self.subtitle_sample_input,
            self.extra_marker_sample_input,
            self.extra_subtitle_prefix_input,
            self.extra_subtitle_sample_input,
            self.regex_input,
            self.split_title_input,
            self.split_subtitle_input,
        ]:
            line_edit.clear()
        self.ebook_publisher_input.setText("안병헌")
        self.language_input.setText("ko")
        self.completed_check.setChecked(True)
        self.rights_input.clear()

        self.sample_text.clear()
        self.extra_sample_text.clear()
        self.preview_text.clear()
        self.log.clear()
        self.ignore_prev_line_check.setChecked(False)
        self.extra_ignore_prev_line_check.setChecked(False)
        self.manual_delete_lines_combo.setCurrentIndex(0)

        self.chapter_table.setRowCount(0)
        self.validation_table.setRowCount(0)
        self.volume_table.setRowCount(0)
        self.isbn_result_table.setRowCount(0)
        self.refresh_isbn_page_buttons()

        self.chapters_per_volume_input.setText("100")
        self.chapters_per_volume_slider.setRange(80, 120)
        self.chapters_per_volume_slider.setValue(100)
        self.volume_after_volume_input.setText("0")
        self.volume_after_volume_slider.setRange(0, 20)
        self.volume_after_volume_slider.setValue(0)
        self.volume_after_count_input.setText("100")
        self.volume_after_count_slider.setRange(80, 120)
        self.volume_after_count_slider.setValue(100)
        self.volume_second_after_volume_input.setText("0")
        self.volume_second_after_volume_slider.setRange(0, 20)
        self.volume_second_after_volume_slider.setValue(0)
        self.volume_second_after_count_input.setText("100")
        self.volume_second_after_count_slider.setRange(80, 120)
        self.volume_second_after_count_slider.setValue(100)

        self.left_tabs.setCurrentIndex(0)
        self.right_stack.setCurrentIndex(0)
        if log:
            self.log.append("초기화 완료")

    def open_isbn_site(self):
        query = self.isbn_search_input.text().strip() or self.title_input.text().strip()
        if not query:
            QMessageBox.information(self, "사이트 이동", "ISBN 검색어를 입력하세요.")
            return
        try:
            QApplication.clipboard().setText(query)
            webbrowser.open("https://www.nl.go.kr/seoji/")
            self.log.append(f"ISBN 사이트 이동: 검색어 클립보드 복사 [{query}]")
        except Exception as e:
            QMessageBox.critical(self, "사이트 이동 오류", str(e))

    def search_isbn(self):
        self.search_isbn_page(1)

    def search_isbn_page(self, page: int, silent: bool = False):
        query = self.isbn_search_input.text().strip() or self.title_input.text().strip()
        if not query:
            if not silent:
                QMessageBox.information(self, "ISBN 검색", "검색어를 입력하세요.")
            return
        if self.isbn_search_thread and self.isbn_search_thread.isRunning():
            self.log.append("ISBN 검색이 이미 진행 중입니다.")
            return

        self.log.append(f"ISBN 검색 중: {query} / {page}페이지")
        self.isbn_search_silent = silent
        self.isbn_search_btn.setEnabled(False)
        self.isbn_site_btn.setEnabled(False)
        self.isbn_search_thread = IsbnSearchThread(query, page, self.isbn_page_unit, self)
        self.isbn_search_thread.finished_ok.connect(self.on_isbn_search_finished)
        self.isbn_search_thread.failed.connect(self.on_isbn_search_failed)
        self.isbn_search_thread.finished.connect(self.on_isbn_search_thread_done)
        self.isbn_search_thread.start()

    def on_isbn_search_finished(self, result: IsbnSearchResult, query: str):
        self.isbn_records = result.records
        self.isbn_current_page = result.current_page
        self.isbn_total_pages = result.total_pages
        self.refresh_isbn_result_table()
        self.refresh_isbn_page_buttons()
        total_text = f" / 총 {result.total_count}건" if result.total_count else ""
        self.log.append(f"ISBN 검색 결과: {query} / {len(self.isbn_records)}건{total_text}")

    def on_isbn_search_failed(self, message: str):
        self.log.append(f"ISBN 검색 실패: {message}")
        if not self.isbn_search_silent:
            QMessageBox.critical(self, "ISBN 검색 오류", message)

    def on_isbn_search_thread_done(self):
        self.isbn_search_btn.setEnabled(True)
        self.isbn_site_btn.setEnabled(True)
        if self.isbn_search_thread:
            self.isbn_search_thread.deleteLater()
            self.isbn_search_thread = None
        self.isbn_search_silent = False

    def refresh_isbn_result_table(self):
        self.isbn_result_table.setRowCount(len(self.isbn_records))
        for row, record in enumerate(self.isbn_records):
            self.isbn_result_table.setItem(row, 0, QTableWidgetItem(record.title))
            self.isbn_result_table.setItem(row, 1, QTableWidgetItem(record.author))
            self.isbn_result_table.setItem(row, 2, QTableWidgetItem(record.publisher))
            self.isbn_result_table.setItem(row, 3, QTableWidgetItem(record.isbn))
            self.isbn_result_table.setItem(row, 4, QTableWidgetItem(record.date))

    def refresh_isbn_page_buttons(self):
        while self.isbn_page_layout.count():
            item = self.isbn_page_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        if self.isbn_total_pages <= 1:
            self.isbn_page_layout.addStretch()
            return

        start_page = max(1, self.isbn_current_page - 5)
        end_page = min(self.isbn_total_pages, start_page + 9)
        start_page = max(1, end_page - 9)

        if self.isbn_current_page > 1:
            prev_btn = QPushButton("이전")
            prev_btn.clicked.connect(lambda: self.search_isbn_page(self.isbn_current_page - 1))
            self.isbn_page_layout.addWidget(prev_btn)

        for page in range(start_page, end_page + 1):
            page_btn = QPushButton(str(page))
            page_btn.setEnabled(page != self.isbn_current_page)
            page_btn.clicked.connect(lambda checked=False, p=page: self.search_isbn_page(p))
            self.isbn_page_layout.addWidget(page_btn)

        if self.isbn_current_page < self.isbn_total_pages:
            next_btn = QPushButton("다음")
            next_btn.clicked.connect(lambda: self.search_isbn_page(self.isbn_current_page + 1))
            self.isbn_page_layout.addWidget(next_btn)

        self.isbn_page_layout.addStretch()

    def apply_selected_isbn(self, *args):
        rows = sorted({index.row() for index in self.isbn_result_table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "선택 없음", "적용할 ISBN 검색 결과를 선택하세요.")
            return
        row = rows[0]
        if not (0 <= row < len(self.isbn_records)):
            return
        try:
            record = self.isbn_records[row]
            self.apply_isbn_record_to_meta(record)
            self.refresh_isbn_result_table()
            self.isbn_result_table.selectRow(row)
            self.log.append(f"메타데이터 적용: {record.title}")
        except Exception as e:
            QMessageBox.critical(self, "메타 적용 오류", str(e))

    def apply_isbn_record_to_meta(self, record: IsbnRecord):
        if record.title:
            self.title_input.setText(self._clean_title_for_meta(record.title))
        if record.author:
            self.author_input.setText(self._clean_author_for_meta(record.author))
        if record.publisher:
            self.publisher_input.setText(record.publisher)
        if record.isbn:
            self.isbn_input.setText(record.isbn)
        if record.date:
            self.date_input.setText(record.date)

    def _clean_author_for_meta(self, author: str) -> str:
        author = re.sub(r"(저자|원작자)\s*:\s*", "", author)
        return author.strip(" ;")

    def _clean_title_for_meta(self, title: str) -> str:
        title = re.sub(r"\s*\(연재\)\s*$", "", title).strip()
        title = re.sub(r"\s*\[연재\]\s*$", "", title).strip()
        title = re.sub(r"^\s*\[연재\]\s*", "", title).strip()
        return title

    def make_volume_preview(self):
        self._make_volume_preview(show_errors=True, log=True)

    def make_volume_preview_silent(self):
        self._make_volume_preview(show_errors=False, log=False)

    def refresh_volume_preview_on_tab(self, index: int):
        if index == getattr(self, "volume_tab_index", -1):
            self.update_volume_after_volume_max()
            self.make_volume_preview_silent()

    def _make_volume_preview(self, show_errors: bool, log: bool):
        try:
            ranges = self.make_volume_ranges_from_inputs()
            if ranges:
                self.total_volumes_input.setText(str(len(ranges)))
            self.volume_table.setRowCount(len(ranges))
            for row, vr in enumerate(reversed(ranges)):
                subset = self.converter.chapters[vr.start_index : vr.end_index + 1]
                bodies = [self.converter.get_chapter_body(c) for c in subset]
                char_count = sum(len(body) for body in bodies)
                estimated_kb = self.estimated_text_kb(bodies)
                self.volume_table.setItem(row, 0, QTableWidgetItem(str(vr.volume)))
                self.volume_table.setItem(row, 1, QTableWidgetItem(subset[0].title if subset else ""))
                self.volume_table.setItem(row, 2, QTableWidgetItem(subset[-1].title if subset else ""))
                volume_count = sum(1 for chapter in subset if chapter.number != 0)
                self.volume_table.setItem(row, 3, QTableWidgetItem(str(volume_count)))
                self.volume_table.setItem(row, 4, QTableWidgetItem(str(char_count)))
                self.volume_table.setItem(row, 5, QTableWidgetItem(f"{estimated_kb} KB"))
                for col in [0, 3, 4, 5]:
                    self.volume_table.item(row, col).setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if log:
                self.log.append(f"권 분할표 생성: {len(ranges)}권")
        except Exception as e:
            if show_errors:
                QMessageBox.critical(self, "오류", str(e))

    def estimated_text_kb(self, texts: List[str]) -> int:
        byte_count = sum(len(text.encode("utf-8")) for text in texts)
        return max(1, round(byte_count / 1024)) if byte_count else 0

    def make_volume_ranges_from_inputs(self) -> List[VolumeRange]:
        return self.converter.make_volume_ranges(
            self.number_input_value(self.chapters_per_volume_input, 100, 1),
            from_volume=self.number_input_value(self.volume_after_volume_input, 0, 0),
            after_chapters_per_volume=self.number_input_value(self.volume_after_count_input, 100, 0),
            second_from_volume=self.number_input_value(self.volume_second_after_volume_input, 0, 0),
            second_after_chapters_per_volume=self.number_input_value(self.volume_second_after_count_input, 100, 0),
        )

    def sync_volume_controls(self):
        base = self.number_input_value(self.chapters_per_volume_input, 100, 1)
        self.set_slider_near_value(self.chapters_per_volume_slider, base)
        self.set_slider_near_value(self.volume_after_count_slider, base)
        if self.volume_after_count_slider.value() != base:
            self.volume_after_count_slider.setValue(base)
        self.sync_second_volume_count_range()
        self.update_volume_after_volume_max()
        self.make_volume_preview_silent()

    def sync_second_volume_count_range(self):
        base = self.number_input_value(self.chapters_per_volume_input, 100, 1)
        current = self.volume_second_after_count_slider.value()
        second_base = self.number_input_value(self.volume_second_after_count_input, base, 1)
        self.set_slider_near_value(self.volume_second_after_count_slider, second_base)
        if current <= 0:
            self.volume_second_after_count_slider.setValue(base)
        self.update_volume_after_volume_max()

    def set_slider_near_value(self, slider: QSlider, value: int, radius: int = 20, minimum: int = 1):
        slider.setRange(max(minimum, value - radius), value + radius)

    def set_slider_range(self, slider: QSlider, minimum: int, maximum: int):
        slider.setRange(minimum, max(minimum, maximum))

    def set_volume_from_defaults_if_empty(self):
        if not self.converter.chapters:
            return
        base = self.number_input_value(self.chapters_per_volume_input, 100, 1)
        chapter_count = len(self.converter.chapters)
        last_volume = max(1, (chapter_count + base - 1) // base)

        if self.number_input_value(self.volume_after_volume_input, 0, 0) == 0:
            self.set_number_input_value(self.volume_after_volume_input, last_volume)
            self.volume_after_volume_slider.setValue(last_volume)

        after_count = self.number_input_value(self.volume_after_count_input, base, 1)
        second_last_volume = self.estimate_volume_count(
            chapter_count,
            base,
            self.number_input_value(self.volume_after_volume_input, 0, 0),
            after_count,
        )
        if self.number_input_value(self.volume_second_after_volume_input, 0, 0) == 0:
            self.set_number_input_value(self.volume_second_after_volume_input, second_last_volume)
            self.volume_second_after_volume_slider.setValue(second_last_volume)

    def number_input_value(self, number_input: QLineEdit, default: int, minimum: int = 0) -> int:
        text = number_input.text().strip()
        if not text:
            return default
        try:
            return max(minimum, int(text))
        except ValueError:
            return default

    def set_number_input_value(self, number_input: QLineEdit, value: int):
        text = str(value)
        if number_input.text() != text:
            old_state = number_input.blockSignals(True)
            number_input.setText(text)
            number_input.blockSignals(old_state)

    def clamp_number_input(self, number_input: QLineEdit, minimum: int, maximum: int):
        value = self.number_input_value(number_input, minimum, minimum)
        value = min(maximum, max(minimum, value))
        self.set_number_input_value(number_input, value)

    def sync_slider_to_number_input(
        self, slider: QSlider, number_input: QLineEdit, value: int, after_sync=None
    ):
        self.set_number_input_value(number_input, value)
        if after_sync:
            after_sync()

    def sync_number_input_to_slider(
        self, number_input: QLineEdit, slider: QSlider, after_sync=None, minimum: int = 1,
        reset_range: bool = True,
    ):
        text = number_input.text().strip()
        if not text:
            return
        value = self.number_input_value(number_input, slider.value(), minimum)
        if reset_range:
            self.set_slider_near_value(slider, value, minimum=minimum)
        else:
            value = min(slider.maximum(), max(slider.minimum(), value))
        if slider.value() != value:
            slider.setValue(value)
        elif after_sync:
            after_sync()

    def update_volume_after_volume_max(self):
        chapter_count = len(self.converter.chapters)
        base = max(1, self.number_input_value(self.chapters_per_volume_input, 100, 1))
        self.set_volume_from_defaults_if_empty()
        last_volume = max(1, (chapter_count + base - 1) // base) if chapter_count else 1
        self.set_slider_range(self.volume_after_volume_slider, 0, last_volume)
        second_last_volume = self.estimate_volume_count(
            chapter_count,
            base,
            self.number_input_value(self.volume_after_volume_input, 0, 0),
            self.number_input_value(self.volume_after_count_input, base, 1),
        )
        self.set_slider_range(self.volume_second_after_volume_slider, 0, second_last_volume)

    def estimate_volume_count(
        self, chapter_count: int, base_size: int, from_volume: int = 0, after_size: int = 0,
    ) -> int:
        if chapter_count <= 0:
            return 1
        vol = 1
        start = 0
        base_size = max(1, base_size)
        while start < chapter_count:
            current_size = base_size
            if after_size > 0 and from_volume > 0 and vol >= from_volume:
                current_size = after_size
            start += max(1, current_size)
            vol += 1
        return max(1, vol - 1)

    def save_epub(self):
        try:
            meta = self.collect_meta()
            self.apply_zero_episode_range_override(meta)
            path = self._default_epub_path(meta)
            meta["volume_end_text"] = "< 끝 >"
            self.converter.create_epub(output_path=path, **meta)
            QMessageBox.information(self, "완료", f"EPUB 생성 완료:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "오류", str(e))

    def save_volume_epubs(self):
        try:
            output_dir = self._txt_output_dir()
            if not self.converter.volume_ranges:
                self.make_volume_ranges_from_inputs()
            if self.converter.volume_ranges:
                self.total_volumes_input.setText(str(len(self.converter.volume_ranges)))
            meta = self.collect_meta()
            self.apply_zero_episode_range_override(meta)
            created = self.converter.create_volume_epubs(output_dir, meta)
            QMessageBox.information(self, "완료", f"권별 EPUB 생성 완료: {len(created)}개\n{output_dir}")
        except Exception as e:
            QMessageBox.critical(self, "오류", str(e))

    def apply_zero_episode_range_override(self, meta: dict):
        chapter_range = self.converter._episode_range_from_chapters(self.converter.chapters)
        if not chapter_range:
            return

        has_zero_episode = any(chapter.number == 0 for chapter in self.converter.chapters)
        if has_zero_episode or not str(meta.get("episode_range", "")).strip():
            meta["episode_range"] = chapter_range
            self.episode_range_input.setText(chapter_range)

        # 단권 생성 시에도 총 화수가 판권/메타데이터에 표시되도록 자동 입력한다.
        # 0화/프롤로그는 총 화수에서 제외하고, 정규 화수의 최대 번호를 총 화수로 사용한다.
        if not str(meta.get("total_episodes", "")).strip():
            episode_numbers = [
                chapter.number
                for chapter in self.converter.chapters
                if isinstance(chapter.number, int) and chapter.number > 0
            ]
            if episode_numbers:
                total_episodes = str(max(episode_numbers))
                meta["total_episodes"] = total_episodes
                self.total_episodes_input.setText(total_episodes)

    def _txt_output_dir(self) -> str:
        if not self.converter.txt_path:
            raise ValueError("TXT 파일을 먼저 불러오세요.")
        return os.path.dirname(os.path.abspath(self.converter.txt_path))

    def _default_epub_path(self, meta: Optional[dict] = None) -> str:
        output_dir = self._txt_output_dir()
        meta = meta or self.collect_meta()
        filename = self.converter.single_epub_filename(
            meta.get("title", "") or self.converter._default_title(),
            meta.get("creator", ""),
            meta.get("episode_range", ""),
            bool(meta.get("completed", True)),
        )
        return os.path.join(output_dir, filename)

    def collect_meta(self) -> dict:
        return {
            "title": self.title_input.text(),
            "subtitle": self.subtitle_input.text(),
            "creator": self.author_input.text(),
            "translator": self.translator_input.text(),
            "publisher": self.publisher_input.text(),
            "date": self.date_input.text(),
            "ebook_publisher": self.ebook_publisher_input.text(),
            "isbn": self.isbn_input.text(),
            "series": self.series_input.text(),
            "volume_no": self.volume_no_input.text(),
            "total_volumes": self.total_volumes_input.text(),
            "total_episodes": self.total_episodes_input.text(),
            "episode_range": self.episode_range_input.text(),
            "completed": self.completed_check.isChecked(),
            "language": self.language_input.text(),
            "rights": self.rights_input.toPlainText(),
        }

    def refresh_chapter_table(self, chapters: List[Chapter]):
        self.invalidate_volume_preview()
        self.refreshing_chapter_table = True
        try:
            self.chapter_table.setRowCount(len(chapters))
            for row, chapter in enumerate(chapters):
                body_len = len(self.converter.get_chapter_body(chapter)) if self.converter.text else 0
                edited_item = QTableWidgetItem("✓" if chapter.edited_body is not None else "")
                title_item = QTableWidgetItem(chapter.marker)
                subtitle_item = QTableWidgetItem(self.display_chapter_subtitle(chapter))
                line_item = QTableWidgetItem(str(chapter.line_index + 1))
                body_len_item = QTableWidgetItem(str(body_len))
                edited_item.setTextAlignment(Qt.AlignCenter)
                edited_item.setFlags(edited_item.flags() & ~Qt.ItemIsEditable)
                line_item.setFlags(line_item.flags() & ~Qt.ItemIsEditable)
                body_len_item.setFlags(body_len_item.flags() & ~Qt.ItemIsEditable)
                self.chapter_table.setItem(row, 0, edited_item)
                self.chapter_table.setItem(row, 1, title_item)
                self.chapter_table.setItem(row, 2, subtitle_item)
                self.chapter_table.setItem(row, 3, line_item)
                self.chapter_table.setItem(row, 4, body_len_item)
        finally:
            self.refreshing_chapter_table = False
        self.log.append(f"챕터 인식: {len(chapters)}개")
        self.refresh_validation_issues()

    def display_chapter_subtitle(self, chapter: Chapter) -> str:
        return self.converter._join_subtitle_prefix(chapter.subtitle_prefix, chapter.subtitle)

    def clear_chapter_list(self):
        self.converter.chapters = []
        self.converter.volume_ranges = []
        self.converter.rejected_chapter_titles = []
        self.converter.ignored_body_line_indexes = set()
        self.chapter_undo_stack = []
        self.current_preview_chapter_index = None
        self.current_preview_body_start = 0
        self.current_suspected_split_position = None
        self.current_suspected_selection_range = None
        self.refreshing_preview_text = True
        try:
            self.preview_text.setPlainText(self.converter.text)
        finally:
            self.refreshing_preview_text = False
        self.refresh_chapter_table([])
        self.log.append("챕터 목록 클리어 완료")

    def save_chapter_undo_state(self):
        self.chapter_undo_stack.append((
            [Chapter(**vars(ch)) for ch in self.converter.chapters],
            set(self.converter.ignored_body_line_indexes),
        ))

    def manual_split_chapter_from_preview(self):
        try:
            if not self.converter.text:
                raise ValueError("TXT 파일을 먼저 불러오세요.")

            cursor = self.preview_text.textCursor()
            split_cursor = QTextCursor(cursor)
            if cursor.hasSelection():
                split_cursor.setPosition(cursor.selectionStart())
            split_pos = split_cursor.position()
            abs_pos = self.current_preview_body_start + split_pos
            line_index = self.converter.line_index_from_char(abs_pos)
            if not (0 <= line_index < len(self.converter.lines)):
                raise ValueError("선택한 줄을 원문 위치로 찾지 못했습니다.")

            delete_lines = self.manual_delete_lines_combo.currentIndex()
            self.save_chapter_undo_state()

            chapter = Chapter(
                None, "", line_index, self.converter.line_offsets[line_index],
                manual_body_start=True, manual_delete_lines=delete_lines,
                edited_body_delete_lines=delete_lines,
            )
            self.converter.chapters.append(chapter)
            self.converter.chapters = self.converter._finalize_chapter_ranges(self.converter.chapters)
            self.converter.volume_ranges = []
            self.rebuild_manual_ignored_body_lines()
            self.refresh_chapter_table(self.converter.chapters)
            self.select_chapter_row(self.converter.chapters.index(chapter))
            self.log.append(f"수동 챕터 추가: 줄 {line_index + 1} / 삭제 {delete_lines}줄")
        except Exception as e:
            QMessageBox.critical(self, "수동 나누기 오류", str(e))

    def merge_selected_chapter_to_previous(self):
        try:
            rows = sorted({index.row() for index in self.chapter_table.selectedIndexes()})
            row = rows[0] if rows else self.current_preview_chapter_index
            if row is None:
                raise ValueError("붙일 챕터를 먼저 선택하세요.")
            if row <= 0:
                raise ValueError("첫 번째 챕터는 위에 붙일 챕터가 없습니다.")
            if row >= len(self.converter.chapters):
                raise ValueError("선택한 챕터를 찾지 못했습니다.")

            chapter = self.converter.chapters[row]
            previous = self.converter.chapters[row - 1]
            self.save_chapter_undo_state()
            previous_text = self.converter.get_chapter_body(previous)

            ignored = set(self.converter.ignored_body_line_indexes)
            prev_line = self.previous_content_line_index(chapter.line_index)
            prev_ignored_text = ""
            if prev_line is not None and prev_line in ignored and prev_line < len(self.converter.line_offsets):
                prev_start = self.converter.line_offsets[prev_line]
                prev_end = (
                    self.converter.line_offsets[prev_line + 1]
                    if prev_line + 1 < len(self.converter.line_offsets)
                    else len(self.converter.text)
                )
                prev_ignored_text = self.converter.text[prev_start:prev_end].strip()
            chapter_text = self.converter.get_chapter_full_text_for_merge(chapter).strip()
            if prev_ignored_text:
                chapter_text = self.converter.join_text_for_merge(prev_ignored_text, chapter_text)

            for idx in list(ignored):
                if idx >= len(self.converter.line_offsets):
                    continue
                line_start = self.converter.line_offsets[idx]
                if chapter.start_char <= line_start < chapter.end_char:
                    ignored.discard(idx)
            if prev_line is not None:
                ignored.discard(prev_line)

            self.converter.ignored_body_line_indexes = ignored
            self.converter.chapters = self.converter._finalize_chapter_ranges(
                [ch for idx, ch in enumerate(self.converter.chapters) if idx != row]
            )
            if chapter_text:
                merged_text = self.converter.join_text_for_merge(previous_text, chapter_text)
                previous.edited_body = merged_text
                previous.edited_body_delete_lines = previous.manual_delete_lines if previous.manual_body_start else 0
            self.converter.volume_ranges = []
            self.refresh_chapter_table(self.converter.chapters)
            self.select_chapter_row(row - 1)
            self.show_validation()
            self.log.append(f"챕터 붙이기 완료: {chapter.title} → {previous.title}")
        except Exception as e:
            QMessageBox.critical(self, "챕터 붙이기 오류", str(e))

    def update_manual_delete_lines_for_current_chapter(self, index: int):
        if self.refreshing_manual_delete_combo:
            return
        rows = sorted({item.row() for item in self.chapter_table.selectedItems()})
        if not rows and self.current_preview_chapter_index is not None:
            rows = [self.current_preview_chapter_index]
        if not rows:
            return

        changed_rows = []
        for row in rows:
            if not (0 <= row < len(self.converter.chapters)):
                continue
            chapter = self.converter.chapters[row]
            if not chapter.manual_body_start:
                continue
            chapter.manual_delete_lines = index
            changed_rows.append(row)

        if not changed_rows:
            return
        self.rebuild_manual_ignored_body_lines()
        self.refresh_chapter_table(self.converter.chapters)
        for row in changed_rows:
            self.chapter_table.selectRow(row)
        if self.current_preview_chapter_index in changed_rows:
            self.update_preview_from_selection()
        self.log.append(f"수동 삭제 줄 수 변경: {len(changed_rows)}개 / {index}줄")

    def rebuild_manual_ignored_body_lines(self):
        manual_ignored = set()
        for chapter in self.converter.chapters:
            if not chapter.manual_body_start:
                continue
            for idx in range(
                chapter.line_index,
                min(chapter.line_index + chapter.manual_delete_lines, len(self.converter.lines)),
            ):
                manual_ignored.add(idx)
        self.converter.ignored_body_line_indexes = manual_ignored

    def set_chapter_title_from_preview_selection(self):
        self.apply_preview_selection_to_current_chapter("title")

    def set_chapter_subtitle_from_preview_selection(self):
        self.apply_preview_selection_to_current_chapter("subtitle")

    def find_in_preview(self, forward: bool = True):
        keyword = self.body_find_input.text()
        if not keyword:
            return

        text = self.preview_text.toPlainText()
        cursor = self.preview_text.textCursor()
        if forward:
            start = cursor.selectionEnd() if cursor.hasSelection() else cursor.position()
            pos = text.find(keyword, start)
            if pos == -1 and start > 0:
                pos = text.find(keyword, 0)
        else:
            start = cursor.selectionStart() if cursor.hasSelection() else cursor.position()
            pos = text.rfind(keyword, 0, start)
            if pos == -1 and start < len(text):
                pos = text.rfind(keyword)

        if pos == -1:
            QMessageBox.information(self, "본문 찾기", "찾는 문장이 없습니다.")
            return

        cursor.setPosition(pos)
        cursor.setPosition(pos + len(keyword), QTextCursor.KeepAnchor)
        self.preview_text.setTextCursor(cursor)
        self.preview_text.ensureCursorVisible()

    def convert_selected_hanja_to_hangul(self):
        try:
            cursor = self.preview_text.textCursor()
            if not cursor.hasSelection():
                raise ValueError("한글로 바꿀 한자를 선택 챕터 본문에서 드래그하세요.")

            selected = cursor.selectedText()
            converted = hanja.translate(selected, "substitution")
            if converted == selected:
                QMessageBox.information(self, "한글 변환", "변환할 한자를 찾지 못했습니다.")
                return

            cursor.beginEditBlock()
            cursor.insertText(converted)
            cursor.endEditBlock()
            self.preview_text.setTextCursor(cursor)
            self.log.append(f"한자 변환: {selected.replace(chr(8233), ' ')} → {converted.replace(chr(8233), ' ')}")
        except Exception as e:
            QMessageBox.critical(self, "한글 변환 오류", str(e))

    def apply_preview_selection_to_current_chapter(self, target: str):
        try:
            if self.current_preview_chapter_index is None:
                raise ValueError("먼저 챕터 목록에서 챕터를 선택하세요.")
            if not (0 <= self.current_preview_chapter_index < len(self.converter.chapters)):
                raise ValueError("선택된 챕터를 찾지 못했습니다.")

            selected = self.preview_text.textCursor().selectedText().replace("\u2029", "\n").strip()
            if not selected:
                raise ValueError("선택 챕터 본문에서 사용할 텍스트를 드래그하세요.")

            chapter = self.converter.chapters[self.current_preview_chapter_index]
            if target == "title":
                chapter.marker = selected
            else:
                chapter.subtitle = selected
            chapter.title = self._chapter_title_from_manual_parts(
                chapter.marker, self.display_chapter_subtitle(chapter)
            )
            self.update_chapter_table_row(self.current_preview_chapter_index)
            self.invalidate_volume_preview()
            label = "제목" if target == "title" else "부제"
            self.log.append(f"챕터 {label} 지정: {selected}")
        except Exception as e:
            QMessageBox.critical(self, "챕터 정보 지정 오류", str(e))

    def _chapter_title_from_manual_parts(self, marker: str, subtitle: str) -> str:
        marker = marker.strip()
        subtitle = subtitle.strip()
        if marker and subtitle:
            return f"{marker} {subtitle}"
        return marker or subtitle

    def update_chapter_table_row(self, row: int):
        if not (0 <= row < self.chapter_table.rowCount() and row < len(self.converter.chapters)):
            return
        chapter = self.converter.chapters[row]
        self.refreshing_chapter_table = True
        try:
            edited_item = self.chapter_table.item(row, 0)
            title_item = self.chapter_table.item(row, 1)
            subtitle_item = self.chapter_table.item(row, 2)
            body_len_item = self.chapter_table.item(row, 4)
            if edited_item:
                edited_item.setText("✓" if chapter.edited_body is not None else "")
            if title_item:
                title_item.setText(chapter.marker)
            if subtitle_item:
                subtitle_item.setText(self.display_chapter_subtitle(chapter))
            if body_len_item:
                body_len_item.setText(str(len(self.converter.get_chapter_body(chapter))))
        finally:
            self.refreshing_chapter_table = False

    def update_chapter_body_from_preview(self):
        if self.refreshing_preview_text:
            return
        if self.current_preview_chapter_index is None:
            return
        if not (0 <= self.current_preview_chapter_index < len(self.converter.chapters)):
            return

        chapter = self.converter.chapters[self.current_preview_chapter_index]
        edited_text = self.preview_text.toPlainText()
        original_text = self.converter.get_original_chapter_body(chapter)
        if edited_text.strip() == original_text.strip():
            chapter.edited_body = None
            chapter.edited_body_delete_lines = 0
        else:
            chapter.edited_body = edited_text
            chapter.edited_body_delete_lines = chapter.manual_delete_lines if chapter.manual_body_start else 0
        self.update_chapter_table_row(self.current_preview_chapter_index)
        self.invalidate_volume_preview()

    def update_chapter_from_table_item(self, item: QTableWidgetItem):
        if self.refreshing_chapter_table:
            return
        row = item.row()
        column = item.column()
        if column not in {1, 2} or not (0 <= row < len(self.converter.chapters)):
            return

        chapter = self.converter.chapters[row]
        if column == 1:
            marker = item.text().strip()
            chapter.marker = marker
            if item.text() != marker:
                self.refreshing_chapter_table = True
                try:
                    item.setText(marker)
                finally:
                    self.refreshing_chapter_table = False
        else:
            chapter.subtitle = item.text().strip()
            chapter.subtitle_prefix = ""

        chapter.title = self._chapter_title_from_manual_parts(chapter.marker, self.display_chapter_subtitle(chapter))
        self.invalidate_volume_preview()
        self.update_preview_from_selection()

    def update_preview_from_selection(self):
        rows = sorted({index.row() for index in self.chapter_table.selectedIndexes()})
        if not rows:
            return
        row = rows[0]
        if 0 <= row < len(self.converter.chapters):
            chapter = self.converter.chapters[row]
            self.refreshing_manual_delete_combo = True
            try:
                self.manual_delete_lines_combo.setCurrentIndex(max(0, min(3, chapter.manual_delete_lines)))
            finally:
                self.refreshing_manual_delete_combo = False
            body_start, body_end = self.converter.get_chapter_body_range(chapter)
            body = self.converter.get_chapter_body(chapter)
            self.current_preview_chapter_index = row
            self.current_preview_body_start = body_start
            self.current_suspected_split_position = None
            self.current_suspected_selection_range = None
            self.refreshing_preview_text = True
            try:
                self.preview_text.setPlainText(body)
            finally:
                self.refreshing_preview_text = False
            self.log.append(
                f"본문 표시: {chapter.title} / 줄 {chapter.line_index + 1} / 전문 {len(body)}자"
            )

    def preview_chapter_body_with_ignored_lines(self, start_char: int, end_char: int) -> str:
        if not self.converter.ignored_body_line_indexes:
            return self.converter.text[start_char:end_char]

        parts = []
        pos = start_char
        for idx in sorted(self.converter.ignored_body_line_indexes):
            if idx >= len(self.converter.line_offsets):
                continue
            line_start = self.converter.line_offsets[idx]
            if not (start_char <= line_start < end_char):
                continue
            next_start = self.converter.line_offsets[idx + 1] if idx + 1 < len(self.converter.line_offsets) else len(self.converter.text)
            parts.append(self.converter.text[pos:line_start])
            original = self.converter.text[line_start:min(next_start, end_char)].strip()
            if original:
                parts.append(f"<<< {original} >>>\n")
            pos = min(next_start, end_char)
        parts.append(self.converter.text[pos:end_char])
        return "".join(parts)

    def refresh_validation_issues(self):
        self.validation_issues = self.build_validation_issues()
        self.validation_table.setRowCount(len(self.validation_issues))
        for row, issue in enumerate(self.validation_issues):
            self.validation_table.setItem(row, 0, QTableWidgetItem("" if issue.number is None else str(issue.number)))
            self.validation_table.setItem(row, 1, QTableWidgetItem(issue.message))
        if self.validation_issues and not self.validation_table.selectedIndexes():
            self.select_validation_issue_row(0)

    def build_validation_issues(self) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        chapters = self.converter.chapters
        for idx, (prev, cur) in enumerate(zip(chapters, chapters[1:])):
            if not isinstance(prev.number, int) or not isinstance(cur.number, int):
                continue
            if cur.number - prev.number > 1:
                for missing in range(prev.number + 1, cur.number):
                    issues.append(ValidationIssue("누락", missing, idx, f"{prev.number}와 {cur.number} 사이"))
        return issues

    def select_validation_issue(self, item: QTableWidgetItem):
        self.select_validation_issue_row(item.row())

    def select_validation_issue_row(self, row: int):
        if not (0 <= row < len(self.validation_issues)):
            return
        self.validation_table.selectRow(row)
        self.validation_table.scrollToItem(self.validation_table.item(row, 0))
        issue = self.validation_issues[row]
        self.select_chapter_row(issue.chapter_index)
        if issue.kind == "누락" and issue.number is not None:
            self.split_title_input.setText(self._marker_for_missing_number(issue.number, issue.chapter_index))
            self.focus_suspected_split_position(issue.number)
        self.log.append(f"검증 항목 선택: {issue.kind} {issue.message}")

    def select_next_missing_issue(self):
        if self.validation_issues:
            self.select_validation_issue_row(0)

    def select_chapter_row(self, row: int):
        if not (0 <= row < self.chapter_table.rowCount()):
            return
        self.chapter_table.selectRow(row)
        self.chapter_table.scrollToItem(self.chapter_table.item(row, 0))
        self.update_preview_from_selection()

    def _marker_for_missing_number(self, number: int, chapter_index: int) -> str:
        suffix = ""
        if 0 <= chapter_index < len(self.converter.chapters):
            marker = self.converter.chapters[chapter_index].marker
            match = re.match(r"(?:제\s*)?\d{1,5}\s*(화|장|회|차)([\.\)\]]?)", marker)
            if match:
                return f"{number}{match.group(1)}{match.group(2)}"
        return f"{number}화"

    def focus_suspected_split_position(self, missing_number: int):
        text = self.preview_text.toPlainText()
        position = self.find_suspected_split_position(text, missing_number)
        if position is None:
            position = max(0, len(text) // 2)
        self.current_suspected_split_position = position
        selection_start, selection_end = self.suspected_subtitle_selection_range(text, position, missing_number)
        self.current_suspected_selection_range = (selection_start, selection_end)
        cursor = self.preview_text.textCursor()
        cursor.setPosition(selection_start)
        cursor.setPosition(selection_end, QTextCursor.KeepAnchor)
        self.preview_text.setTextCursor(cursor)
        self.preview_text.ensureCursorVisible()

    def suspected_subtitle_selection_range(self, text: str, position: int, missing_number: int) -> tuple[int, int]:
        line_start = text.rfind("\n", 0, position) + 1
        line_end = text.find("\n", position)
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]
        marker_match = re.search(
            rf"(?:제\s*)?{missing_number}\s*(?:화|장|회|차)\s*[\.\)\]]?",
            line,
            re.IGNORECASE,
        )
        if marker_match:
            tail_start = line_start + marker_match.end()
            tail_end = line_end
            while tail_start < tail_end and text[tail_start].isspace():
                tail_start += 1
            if tail_start < tail_end:
                return tail_start, tail_end

        next_start = line_end + 1
        while next_start < len(text):
            next_end = text.find("\n", next_start)
            if next_end == -1:
                next_end = len(text)
            candidate = text[next_start:next_end].strip()
            if candidate:
                if not re.match(r"^(?:제\s*)?\d{1,5}\s*(?:화|장|회|차)\b", candidate, re.IGNORECASE):
                    left_trim = len(text[next_start:next_end]) - len(text[next_start:next_end].lstrip())
                    right_trim = len(text[next_start:next_end].rstrip())
                    return next_start + left_trim, next_start + right_trim
                break
            next_start = next_end + 1

        return line_start, line_end

    def find_suspected_split_position(self, text: str, missing_number: int) -> Optional[int]:
        patterns = [
            rf"(?:제\s*)?{missing_number}\s*(?:화|장|회|차)\s*[\.\)\]]?.*",
            rf"^\s*.{{0,30}}{missing_number}\s*(?:화|장|회|차)\s*[\.\)\]]?.*$",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
            if match:
                return match.start()
        return None

    def set_split_subtitle_from_selection(self):
        selected = self.preview_text.textCursor().selectedText().replace("\u2029", "\n").strip()
        if not selected:
            QMessageBox.information(self, "선택 없음", "본문에서 부제로 사용할 문장을 선택하세요.")
            return
        self.split_subtitle_input.setText(selected)

    def split_chapter_from_preview(self):
        try:
            if self.current_preview_chapter_index is None:
                raise ValueError("먼저 챕터 또는 검증 항목을 선택하세요.")
            title = self.split_title_input.text().strip()
            if not title:
                raise ValueError("나눌 회차 제목을 입력하세요. 예: 771화.")

            cursor = self.preview_text.textCursor()
            if (
                self.current_suspected_split_position is not None
                and self.current_suspected_selection_range is not None
                and cursor.hasSelection()
                and (cursor.selectionStart(), cursor.selectionEnd()) == self.current_suspected_selection_range
            ):
                pos = self.current_suspected_split_position
            else:
                pos = cursor.selectionStart() if cursor.hasSelection() else cursor.position()
            abs_pos = self.current_preview_body_start + pos
            line_index = self.converter.line_index_from_char(abs_pos)
            self.save_chapter_undo_state()
            new_chapter = self.converter.insert_chapter_at_line(
                line_index,
                title,
                self.split_subtitle_input.text(),
            )
            self.refresh_chapter_table(self.converter.chapters)
            new_row = self.converter.chapters.index(new_chapter)
            self.select_chapter_row(new_row)
            self.show_validation()
            self.select_next_missing_issue()
            self.log.append(
                f"챕터 분할 완료: {new_chapter.title} / 기준 줄 {line_index + 1}"
            )
        except Exception as e:
            QMessageBox.critical(self, "챕터 분할 오류", str(e))

    def undo_chapter_split(self):
        if not self.chapter_undo_stack:
            QMessageBox.information(self, "되돌리기", "되돌릴 챕터 분할이 없습니다.")
            return
        chapters, ignored_body_line_indexes = self.chapter_undo_stack.pop()
        self.converter.chapters = self.converter._finalize_chapter_ranges(chapters)
        self.converter.ignored_body_line_indexes = ignored_body_line_indexes
        self.refresh_chapter_table(self.converter.chapters)
        self.show_validation()
        self.select_next_missing_issue()
        self.log.append("챕터 분할 되돌리기 완료")

    def invalidate_volume_preview(self):
        self.volume_table.setRowCount(0)

    def _selected_sample_text(self) -> str:
        cursor_info = self._selected_sample_cursor_info()
        selected = cursor_info[1] if cursor_info else ""
        return selected.replace("\u2029", "\n").strip()

    def _selection_from_editor(self, editor: QTextEdit) -> tuple[str, Optional[int]]:
        cursor = editor.textCursor()
        selected = cursor.selectedText().replace("\u2029", "\n").strip()
        position = cursor.selectionStart() if cursor.hasSelection() else None
        return selected, position

    def _selected_sample_line(self) -> Optional[int]:
        cursor_info = self._selected_sample_cursor_info()
        if not cursor_info:
            return None
        cursor = cursor_info[0]
        return cursor.selectionStart() if cursor.hasSelection() else cursor.position()

    def _selected_sample_cursor_info(self):
        for editor in [self.sample_text, getattr(self, "extra_sample_text", None)]:
            if editor is None:
                continue
            cursor = editor.textCursor()
            selected = cursor.selectedText()
            if selected:
                return cursor, selected
        focused = QApplication.focusWidget()
        if isinstance(focused, QTextEdit):
            cursor = focused.textCursor()
            return cursor, cursor.selectedText()
        return None

    def _uses_next_line_subtitle(self, extra: bool) -> bool:
        marker_pos = self.extra_marker_selection_line if extra else self.marker_selection_line
        subtitle_pos = self.extra_subtitle_selection_line if extra else self.subtitle_selection_line
        sample = self.extra_sample_text if extra else self.sample_text
        if marker_pos is None or subtitle_pos is None:
            return False
        text = sample.toPlainText()
        marker_line = text.count("\n", 0, marker_pos)
        subtitle_line = text.count("\n", 0, subtitle_pos)
        if subtitle_line < marker_line:
            return False
        return subtitle_line > marker_line

    def show_validation(self):
        self.log.append("--- 챕터 검증 ---")
        for w in self.converter.validate_chapters():
            self.log.append(w)


def main():
    import sys
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
