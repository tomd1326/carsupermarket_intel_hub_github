# Self-Analytics report file

How a Claude chat hands Tom a ready-made report for the Self-Analytics page
(`/self-analytics`). Write the SQL, check it against `PROD_CARSUPERMARKET_DB` with the
`carsupermarket_snowflake_mcp` tools, then give Tom one JSON file in the format below. Tom
clicks **Import** on the page and pastes it (or chooses the `.json` file). A report with the
same name and folder as one of his is updated, not added twice, so a corrected version can
simply be imported again. **Export** on the page gives the same format back.

The checks are in `utils/self_analytics.py` (`clean_report`, `_clean_chart`, `check_sql`).

## The file

```json
{
  "format": "self_analytics_report",
  "version": 1,
  "name": "Prices By Month",
  "folder": "Pricing",
  "description": "Average purchase, RRP and selling prices of cars invoiced since August, by month.",
  "visibility": "chosen",
  "users": ["jane.smith", "john.doe"],
  "show_table": false,
  "sql": "SELECT invoice_posted_date, supplier_channel, purchase_price_inc_vat, sale_price_before_discount_inc_admin_fee FROM PROD_CARSUPERMARKET_DB.PRODUCT_SALES_PERFORMANCE.SALES_PERFORMANCE WHERE invoice_posted_date > '2026-08-01' AND invoice_credited = FALSE",
  "charts": [
    {
      "title": "Average Prices By Month",
      "type": "line",
      "x": "INVOICE_POSTED_DATE",
      "grain": "month",
      "measures": [
        {"column": "PURCHASE_PRICE_INC_VAT", "agg": "avg", "label": "Purchase"},
        {"column": "SALE_PRICE_BEFORE_DISCOUNT_INC_ADMIN_FEE", "agg": "avg", "label": "Sale"}
      ],
      "format": "currency"
    },
    {
      "title": "Sale Minus Purchase, By Channel",
      "type": "bar",
      "x": "INVOICE_POSTED_DATE",
      "grain": "month",
      "split": "SUPPLIER_CHANNEL",
      "measures": [
        {"column": "SALE_PRICE_BEFORE_DISCOUNT_INC_ADMIN_FEE", "agg": "avg", "minus": "PURCHASE_PRICE_INC_VAT"}
      ],
      "format": "currency"
    },
    {
      "title": "Cars Invoiced",
      "type": "tile",
      "measures": [{"column": "INVOICE_POSTED_DATE", "agg": "count"}]
    }
  ]
}
```

Several reports at once: `{"reports": [ {...}, {...} ]}` (up to 50).

## Report fields

| Field | Needed | Values |
|---|---|---|
| `name` | Yes | Up to 120 characters |
| `sql` | Yes | One `SELECT` or `WITH` query. No `;` between statements, no Snowflake AI functions (`CORTEX`, `AI_...`). Use fully qualified names: `PROD_CARSUPERMARKET_DB.SCHEMA.TABLE` |
| `folder` | No | Groups reports in the list |
| `description` | No | Shown under the name, up to 3,000 characters |
| `visibility` | No | `only_me` (default), `everyone` (everyone with the page), `chosen` (the `users` list) |
| `users` | With `chosen` | Usernames (sign-in names). They also need the page ticked in Settings > Page Access |
| `show_table` | No | `true` shows the rows under the charts to people who only look |
| `charts` | No | List of charts, below. Up to 40 |

People who only look never see the SQL. The rows are re-read from Snowflake when the report is
opened, reused for the cache time set in Page Settings (15 minutes to start), and Refresh gets the
latest.

## Chart fields

Column names must match the query's column names. Snowflake returns them in capitals unless
quoted, so write them in capitals.

| Field | Values |
|---|---|
| `title`, `subtitle` | Text. A title is made from the values if left out |
| `type` | `bar` (column), `stacked_bar`, `horizontal_bar`, `line`, `area`, `pie`, `doughnut`, `scatter`, `table`, `tile` (one big number) |
| `x` | The column along the bottom. Leave out for one total |
| `grain` | For a date `x`: `none`, `day`, `week`, `month`, `quarter`, `year` |
| `split` | A column whose values become separate series |
| `measures` | List of values: `column`, `agg` (`sum`, `avg`, `count`, `count_distinct`, `min`, `max`, `median`), optional `minus` (a second column taken away), `as_pct` (show the difference as a % of the second column), `label`, `color` (`#rrggbb`) |
| `filters` | List of `column`, `op` (`=`, `!=`, `>`, `>=`, `<`, `<=`, `contains`, `not_contains`, `is_empty`, `not_empty`), `value` |
| `sort` | `x` (default), `value_desc`, `value_asc` |
| `top_n` | Show only the largest N (0 = all) |
| `format` | `number`, `currency`, `percent` |
| `decimals` | 0 to 4 |
| `from_zero`, `data_labels`, `legend` | `true` / `false` |
| `width` | Columns of the grid the chart spans, 1 to 4 |

A `tile` uses only its first value and its filters, worked out over every row.
