# Obeya charts for the KPI page

Adds the Daily OBEYA Targets workbook's 57 charts to the Intelligence Hub KPI page, with the
workbook's targets. Every chart is added **Admins Only**: no one else is sent it until it is
released with **Show To Everyone** on the chart, or the **Everyone** tick in Settings > Charts.

| File | What it is |
|---|---|
| `add_obeya_charts.template.js` | The chart definitions. Edit this, not the built file |
| `build_add_obeya_charts.py` | Builds `add_obeya_charts.js` with the workbook's `cs_daily` targets put in |
| `add_obeya_charts.js` | The built script you paste into the browser console |

Figures come from `daily_summary` wherever it has them. Only DOC Orders, Staff Numbers,
Outbound calls, Price Changes and AutoConvert CS / CSMF are read from the DSE (Daily Spend and
Enquiries) SharePoint file, until Snowflake has them.

## Steps

1. Deploy the app change first (Admins Only), or the charts are visible to everyone.
2. If the workbook targets have changed, rebuild: `python build_add_obeya_charts.py "<workbook path>"`.
3. Open the KPI page as the admin in Admin View, press Load Data and wait for the SharePoint files.
4. F12 > Console, paste all of `add_obeya_charts.js`, press Enter. It lists each chart as `ADD`,
   `already there` or `skip - not found`, with its section, data and target. Nothing changes.
5. Change `const DRY_RUN = true;` to `false` at the top, paste again, then reload the page.
   A backup of the saved layout downloads first.

Running it again never adds a chart twice (same title, or same metrics and division).
A chart whose section is locked to one data source goes into a new `<name> (Obeya)` section.
A single-metric chart's fixed or per-date targets belong to the metric, so any already stored
are kept.
