"""Minimal, fast streaming .xlsx writer for the All Details export.

openpyxl spends ~0.35 ms per 31-column row building cell objects; this writes
the sheet XML text directly into the zip as rows arrive (several times faster,
constant memory). It supports exactly what the export needs:
  * several sheets, each with a bold, frozen header row;
  * numbers, text (inline strings) and dates (DD-MM-YYYY).

Usage:
    with XlsxStreamWriter(path) as xw:
        xw.start_sheet('All details', header)
        xw.write_row(['JCPL01', 10, date(2026, 11, 3), ...])
"""

import re
import zipfile
from datetime import date
from xml.sax.saxutils import escape

_EPOCH = date(1899, 12, 30).toordinal()      # Excel day 0 (1900 date system)
_BAD_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')
_FLUSH_ROWS = 2000


def _col(n):
    s = ''
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _text(v):
    t = str(v)
    if _BAD_XML.search(t):
        t = _BAD_XML.sub('', t)
    return escape(t)


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    '{sheets}</Types>')
_SHEET_CT = ('<Override PartName="/xl/worksheets/sheet{n}.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>')
_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<bookViews><workbookView/></bookViews><sheets>{sheets}</sheets></workbook>')
_WB_SHEET = '<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>'
_WB_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '{sheets}<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '</Relationships>')
_WB_REL = ('<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
           'relationships/worksheet" Target="worksheets/sheet{n}.xml"/>')
# style 0 = normal, 1 = date DD-MM-YYYY, 2 = bold (header)
_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="DD-MM-YYYY"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>')
_SHEET_HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<sheetViews><sheetView workbookViewId="0">'
    '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
    '</sheetView></sheetViews><sheetData>')
_SHEET_TAIL = '</sheetData></worksheet>'


class XlsxStreamWriter:
    def __init__(self, path, compresslevel=6):
        self._zip = zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, compresslevel=compresslevel)
        self._names = []
        self._fh = None
        self._buf = []
        self._row = 0
        self._cols = []

    # ---- sheets
    def start_sheet(self, name, header):
        self._end_sheet()
        self._names.append(name)
        n = len(self._names)
        self._fh = self._zip.open(f'xl/worksheets/sheet{n}.xml', 'w', force_zip64=True)
        self._fh.write(_SHEET_HEAD.encode())
        self._cols = [_col(i + 1) for i in range(len(header))]
        self._row = 0
        self._emit(header, style=2)

    def _end_sheet(self):
        if self._fh is not None:
            self._flush()
            self._fh.write(_SHEET_TAIL.encode())
            self._fh.close()
            self._fh = None

    # ---- rows
    def write_row(self, values):
        self._emit(values)
        if len(self._buf) >= _FLUSH_ROWS:
            self._flush()

    def _emit(self, values, style=0):
        self._row += 1
        r = self._row
        cols = self._cols
        st = f' s="{style}"' if style else ''
        parts = [f'<row r="{r}">']
        for i, v in enumerate(values):
            if v is None or v == '':
                continue
            ref = f'{cols[i]}{r}'
            if isinstance(v, bool):
                parts.append(f'<c r="{ref}"{st} t="inlineStr"><is><t>{v}</t></is></c>')
            elif isinstance(v, (int, float)):
                parts.append(f'<c r="{ref}"{st}><v>{v}</v></c>')
            elif isinstance(v, date):
                parts.append(f'<c r="{ref}" s="1"><v>{v.toordinal() - _EPOCH}</v></c>')
            else:
                parts.append(f'<c r="{ref}"{st} t="inlineStr"><is><t xml:space="preserve">'
                             f'{_text(v)}</t></is></c>')
        parts.append('</row>')
        self._buf.append(''.join(parts))

    def _flush(self):
        if self._buf:
            self._fh.write(''.join(self._buf).encode('utf-8'))
            self._buf = []

    # ---- finish
    def close(self):
        self._end_sheet()
        n = len(self._names)
        z = self._zip
        z.writestr('[Content_Types].xml', _CONTENT_TYPES.format(
            sheets=''.join(_SHEET_CT.format(n=i) for i in range(1, n + 1))))
        z.writestr('_rels/.rels', _ROOT_RELS)
        z.writestr('xl/workbook.xml', _WORKBOOK.format(sheets=''.join(
            _WB_SHEET.format(name=escape(nm, {'"': '&quot;'}), n=i)
            for i, nm in enumerate(self._names, 1))))
        z.writestr('xl/_rels/workbook.xml.rels', _WB_RELS.format(
            sheets=''.join(_WB_REL.format(n=i) for i in range(1, n + 1))))
        z.writestr('xl/styles.xml', _STYLES)
        z.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.close()
        else:
            try:
                self._zip.close()
            except Exception:                        # noqa: BLE001
                pass
