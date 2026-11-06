"""SMMT files (utils/smmt.py): reading a marque sheet, the most recent file winning,
working a month out from year to date, the final-figures note and the S3 folders.

Run from source_code/router/backend:
    poetry run pytest tests/test_smmt.py
"""

import datetime
import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import openpyxl

from utils import smmt


def _sheet(year, rows, total):
    """A Cars_MM_YYYY workbook: rows are (marque, month, last_year, ytd, ytd_last_year)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'CARS_MONTH_END'
    ws.append(['REGISTRATIONS OF NEW CARS IN THE UNITED KINGDOM - BY MARQUE'])
    ws.append(['October', '% Change', 'YEAR-TO-DATE', '% Change'])
    ws.append(['MARQUE', year, '% Market share', year - 1, '% Market share', None,
               year, '% Market share', year - 1, '% Market share', None])
    for name, m, ly, ytd, ytd_ly in rows + [total]:
        ws.append([name, m, 1.0, ly, 1.0, 0, ytd, 1.0, ytd_ly, 1.0, 0])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _zip(name, data):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(name, data)
    return buf.getvalue()


def _file(file_id, month, rows, total):
    year, mm = int(month[:4]), month[5:7]
    data = _zip(f'Cars_{mm}_{year}.xlsx', _sheet(year, rows, total))
    info = smmt.classify('new_car_registrations', f'{month}.zip', data)
    return {'file_id': file_id, 'marques': info['marques'], 'tracker': []}


def test_marque_sheet_is_read_from_the_zip():
    data = _zip('Cars_10_2026.xlsx', _sheet(2026, [('Audi', 8125, 9314, 96751, 93901)],
                                            ('Grand Total', 152730, 144945, 1800000, 1700000)))
    info = smmt.classify('new_car_registrations', 'October-registrations-2026.zip', data)
    assert info['kind'] == 'marque_sheet'
    assert info['month'] == '2026-10'
    audi, total = info['marques']['rows']
    assert (audi['marque'], audi['month'], audi['last_year'], audi['ytd']) == ('Audi', 8125, 9314, 96751)
    assert total['marque'] == smmt.TOTAL and total['month'] == 152730


def test_most_recent_file_replaces_an_earlier_figure():
    first = _file(1, '2025-10', [('Audi', 100, 90, 1000, 900)], ('Total', 100, 90, 1000, 900))
    # A year later SMMT restates October 2025 as 105.
    later = _file(2, '2026-10', [('Audi', 120, 105, 1200, 1005)], ('Total', 120, 105, 1200, 1005))
    out = smmt.combine_registrations([first, later])
    assert out['marques'][('2025-10', 'Audi')]['units'] == 105
    assert out['marques'][('2025-10', 'Audi')]['how'] == smmt.HOW_LAST_YEAR
    assert out['totals']['2025-10']['units'] == 105


def test_smmt_file_beats_the_tracker():
    tracker = {'file_id': 9, 'marques': None,
               'tracker': [{'month': '2025-10', 'private': 40, 'fleet': 50, 'total': 90}]}
    sheet = _file(1, '2025-10', [('Audi', 100, 90, 1000, 900)], ('Total', 100, 90, 1000, 900))
    out = smmt.combine_registrations([tracker, sheet])
    assert out['totals']['2025-10']['units'] == 100
    assert out['totals']['2025-10']['private'] == 40


def test_missing_month_worked_out_from_year_to_date():
    jan = _file(1, '2026-01', [('Audi', 10, 0, 10, 0)], ('Total', 10, 0, 10, 0))
    mar = _file(2, '2026-03', [('Audi', 30, 0, 60, 0)], ('Total', 30, 0, 60, 0))
    out = smmt.combine_registrations([jan, mar])
    feb = out['marques'][('2026-02', 'Audi')]
    assert feb['units'] == 20 and feb['how'] == smmt.HOW_YTD


def test_two_missing_months_are_not_guessed():
    jan = _file(1, '2026-01', [('Audi', 10, 0, 10, 0)], ('Total', 10, 0, 10, 0))
    apr = _file(2, '2026-04', [('Audi', 30, 0, 90, 0)], ('Total', 30, 0, 90, 0))
    out = smmt.combine_registrations([jan, apr])
    assert ('2026-02', 'Audi') not in out['marques']
    assert ('2026-03', 'Audi') not in out['marques']


def test_final_figures_note_and_date():
    html = '<p>SMMT preliminary figures are subject to change. Full and final figures published Monday 5 October, 9am.</p>'
    note = smmt.page_note(html)
    assert note == 'Full and final figures published Monday 5 October, 9am'
    assert smmt.final_date(note, datetime.date(2026, 10, 2)) == datetime.date(2026, 10, 5)
    assert smmt.final_date(note, datetime.date(2026, 11, 6)) == datetime.date(2026, 10, 5)


def test_month_from_text_and_folders():
    assert smmt.month_from_text('Sep-25-car-registrations') == '2025-09'
    assert smmt.month_from_text('SMMT NEW CAR REGISTRATIONS April 23 - FINAL') == '2023-04'
    assert smmt.month_from_text('2023_09') == '2023-09'
    assert smmt.quarter_from_text('Used-Cars-Q2-2026') == '2026-Q2'
    assert smmt.feed_for('Car-colours-2025.zip', 'new_car_registrations', smmt.DEFAULT_SKIP_WORDS) is None
    assert smmt.feed_for('WEBSUM-SMMT-CAR-LCV-MARKET-OUTLOOK-Q4.docx', 'outlook', ['colour']) == 'outlook'
    # A post comes out the month after its data.
    assert smmt.period_for('new_car_registrations', {}, 'release.zip', '2026-03-05') == '2026-02'
    assert smmt.s3_key('outlook', '2026-11-05', 'a b.docx') == smmt.S3_ROOT + '/outlook/2026-11-05/a b.docx'
