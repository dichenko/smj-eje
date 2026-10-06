from datetime import date
from io import BytesIO

import xlsxwriter


def video_report(video):
    """Build the downloadable one-video report as an in-memory Excel workbook."""
    output = BytesIO()
    workbook = xlsxwriter.Workbook(output, {
        "in_memory": True,
        "strings_to_formulas": False,
        "strings_to_urls": False,
    })
    sheet = workbook.add_worksheet("Отчет")
    sheet.hide_gridlines(2)
    sheet.freeze_panes(1, 0)
    sheet.set_landscape()
    sheet.fit_to_pages(1, 1)
    sheet.set_margins(0.35, 0.35, 0.5, 0.5)
    sheet.set_column("A:A", 20)
    sheet.set_column("B:C", 25)
    sheet.set_column("D:D", 14)
    sheet.set_column("E:F", 16)
    sheet.set_column("G:G", 48)

    header = workbook.add_format({
        "bold": True, "font_color": "#FFFFFF", "bg_color": "#17365D",
        "text_wrap": True, "valign": "vcenter", "border": 0,
    })
    cell = workbook.add_format({"text_wrap": True, "valign": "top", "border": 1,
                                "border_color": "#D9E2F3"})
    date_cell = workbook.add_format({"num_format": "dd.mm.yyyy", "valign": "top",
                                     "border": 1, "border_color": "#D9E2F3"})
    link_cell = workbook.add_format({"font_color": "#0563C1", "underline": 1,
                                     "text_wrap": True, "valign": "top",
                                     "border": 1, "border_color": "#D9E2F3"})
    section = workbook.add_format({"bold": True, "font_color": "#17365D", "bg_color": "#EAF1F8",
                                  "valign": "vcenter", "border": 1,
                                  "border_color": "#D9E2F3"})
    notes = workbook.add_format({"text_wrap": True, "valign": "top", "border": 1,
                                 "border_color": "#D9E2F3"})

    headings = ["Филиал", "Преподаватель", "Координатор", "Модуль",
                "Дата запроса", "Дата отправки", "Ссылка на видео"]
    for col, value in enumerate(headings):
        sheet.write_string(0, col, value, header)
    sheet.set_row(0, 32)
    values = [video["city"], video["teacher"], video.get("coordinator_name") or "",
              video["module"]]
    for col, value in enumerate(values):
        sheet.write_string(1, col, str(value), cell)
    for col, key in ((4, "request_date"), (5, "sent_date")):
        value = video.get(key)
        if value:
            year, month, day = (int(part) for part in value.split("-"))
            sheet.write_datetime(1, col, date(year, month, day), date_cell)
        else:
            sheet.write_blank(1, col, None, date_cell)
    url = str(video.get("video_url") or "")
    if url.startswith(("https://", "http://")):
        sheet.write_url(1, 6, url, link_cell, string=url)
    else:
        sheet.write_string(1, 6, url, cell)
    sheet.set_row(1, 42)

    for row, label, text in (
        (3, "Что хорошо:", video.get("positive_notes") or ""),
        (9, "Зона роста:", video.get("growth_notes") or ""),
    ):
        sheet.merge_range(row, 0, row, 6, label, section)
        sheet.merge_range(row + 1, 0, row + 4, 6, str(text), notes)
        line_count = max(1, str(text).count("\n") + 1)
        sheet.set_row(row + 1, min(240, max(72, 18 * line_count)))
    sheet.print_area("A1:G14")
    workbook.close()
    output.seek(0)
    return output
