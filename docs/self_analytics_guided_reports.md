# Guided Self-Analytics

The guided builder uses approved website knowledge and explicit user choices.
It does not call an AI provider, use Snowflake AI, or interpret arbitrary prose
as executable SQL. A prompt finds relevant knowledge; the user chooses the
source, fields, dates, population, calculations, charts and filters.

## Enabling

The feature starts **off** in Self-Analytics Settings > Enable Guided Reports.
This protects existing reports while the deployed database permissions, live
sources and browser behavior are verified. No dbt changes are required.

On the first knowledge read after deployment, the versioned project bundle is
inserted into `ai_knowledge` as 21 **Proposed** entries: 19 rules and two report
source definitions. Existing IDs are never overwritten. An import marker in
`ai_page_settings` prevents later cold starts from restoring deleted entries.
The import is transactional and safe to retry if interrupted. This code change
does not itself connect to or populate the live database.

In Semantic & Context Layer, review the entries in the Report Building category
and approve the applicable rules and source definitions. `Add Starting Entries`
also includes this bundle. Existing active website entries remain available;
disabled, hidden and proposed entries cannot drive guided reports.

The two initial definitions are Sales And Buyer Comparisons and Daily Business
Metrics. They were checked against repository SQL, not a live Snowflake schema.
Each table entry has an editable **Guided Report Source Definition (JSON)** field:

```json
{
  "table": "PROD_CARSUPERMARKET_DB.PRODUCT_SLT_KPIS.DAILY_SUMMARY",
  "grain": "One Calendar Day",
  "columns": [
    {"name": "date", "type": "date"},
    {"name": "orders", "type": "number"}
  ],
  "questions": []
}
```

Column types are `number`, `text`, `date`, `datetime` and `bool`. Column and
table names must be ordinary Snowflake identifiers, not SQL expressions.
Definitions may include required questions with a column, label, and choices.
Each choice has an `id`, `label` and optionally an `op` and `value` for a filter;
a choice with no value explicitly includes all values. Define business
calculations upstream or add a verified source column; prose is not executed.

## User Flow

1. Enter the request and choose Find Knowledge And Sources. Read matching
   approved definitions and their source references.
2. Choose a source and load its fields. Select the exact measures, dimensions
   and drill-down details. No Snowflake query has run at this point.
3. Confirm the event date and inclusive date range. Answer the source questions
   (sales status, credits and sales type for the sales source). Add any other
   population filters; no consignment, buyback or finance-switch rule is guessed.
4. Add charts using the chart editor. Confirm chart type, grouping, axis fields,
   split, measures, units, comparison population, filters and appearance.
   Chart cards use the existing Self-Analytics theme and page appearance settings.
   There are no automatic targets or red/green performance bands.
5. Review the complete interpretation and confirm it before building. Changing
   the plan invalidates confirmation; the server checks the review hash again.
6. Save the report. Saved guided reports retain their source choices, original
   request and charts. Existing report permissions and sharing still apply.
   A saved report can run by ID for an authorized viewer. Changing an approved
   source definition requires the author to load it again and review the report.

Users need the existing Run Own Queries permission to build reports. This does
not grant another role or broaden who can see saved reports. Reading workbook
sources uses the site's existing service credentials; owners must share reports
with the appropriate users.

## Calculations And Drill-Down

- All calculations use underlying selected rows. Interactive filtering therefore
  recomputes an average rather than averaging subgroup averages.
- Matched Rows excludes a row if any selected measure/comparison operand is
  missing. Available Values keeps each measure's own denominator. Tooltips show
  nonempty primary-value counts, and matched charts show their cohort count.
- Zero is a value, not automatically missing. Add explicit filters if a source
  uses zero as a missing-value marker. No prep-estimate or price fallback is inferred.
- Minus means the difference of the selected aggregates. As % means that
  difference divided by the absolute second aggregate, multiplied by 100.
  For like-for-like comparisons, select Matched Rows.
- A Ratio Denominator uses sum(numerator) / sum(denominator) * 100. Choose Total
  and Percent; do not combine this with Minus. A zero denominator yields blank.
- Click a bar, line point or pie segment to inspect the same grouping, split,
  report filters and comparison cohort. The popup obeys the existing table-row
  display limit. It is a source-row view, not a new live vehicle lookup.
- Too many rows or too much response data stops the report. Truncated data is
  never accepted for a guided chart. Narrow dates/filters or use an appropriately
  modeled aggregated source; simply averaging pre-averaged columns is not a fix.

## SharePoint

Enabled entries from Settings > SharePoint are offered, including Daily Spend
And Enquiries when configured. The builder reuses `sharepoint_sources.get_copy`;
it does not download arbitrary pasted URLs or add a separate file cache.

Headers are read from the actual cached file. Duplicate/empty headers block the
source. The registered date header identifies the workbook date; ISO dates and
Excel date serials are supported. Ambiguous date strings stop the report. Check
exact headers, row meaning, units, duplicate handling and ownership before use.
Numeric calculations require a numeric source column; numeric-looking identifiers
must stay text. Rows with missing report dates are excluded, as with SQL date filters.

A missing, pending or failed copy blocks building. Its modification time is shown
in the review and response. Refresh the shared copy using Settings > SharePoint
when needed. Reading live Snowflake at run time does not establish when upstream
models last refreshed; that is a separate data check.

## Boundaries And Missing Data

One dataset is used per report. There is no inferred cross-source join, arbitrary
formula language, secondary value axis, target coloring or automatic data ingestion.
Pie/doughnut and number tiles require one measure without a series split; scatter
requires two raw numeric columns. Date weeks start on Monday. Each chart can have
its own filters and matched cohort. A report can contain up to 40 charts.

Not finding a field here means **not mapped**, not **absent from Snowflake**. Check
the current dbt model or live schema before claiming absence. Confirm the exact
metric, period, grain and join key. Marketing's workbook is a possible source for
spend/enquiries; Cars Bought for original estimates; AutoView for actual costs;
finance for subsidies. The existing `uk_sharepoint_to_s3` project is a possible
ingestion route, not evidence a given workbook is already ingested.

The local buyer comparison HTML is a complexity reference. Its historical fee,
custom channel mapping, May-October dates, buyback exclusions and prep fallback
have not been copied into universal rules. Equivalent current source columns
are offered where verified. Exact historical recreation still requires the
user to confirm and supply any unmapped definitions.

## Verification

Python tests cover source approval, missing choices, identifier and literal
validation, report round trips, confirmation changes, disabled-feature behavior,
row limits, SharePoint date/identifier handling and additive knowledge import.
JavaScript tests cover matched cohorts, unequal denominators, filtering, rates,
drill-down and missing columns. No live Snowflake query, SharePoint download,
database migration or browser visual check was performed during implementation.
