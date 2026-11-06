# Project Knowledge Review — 6 November 2026

The discovery pass found 47 project Markdown files under `C:\Git`, excluding
Git metadata, virtual environments, dependencies, dbt package copies, compiled
targets and caches. Relevant material was curated into
`source_code/router/backend/data/ai_knowledge_seed/report_builder_entries.json`.
This is a review snapshot, not an automatic document watcher or a claim that
every sentence in project documentation is an approved business rule.

## Inventory And Treatment

| Files | Count | Treatment |
| --- | ---: | --- |
| `.claude/CLAUDE.md`, `workflow.md`, `uk_dbt.md`, `ingestion.md`, `carsupermarket_intel_hub.md` | 5 | Business meaning, dates, prices, status, costs, grain, source ownership and freshness retained. Assistant/git permissions and deployment instructions excluded. |
| `carsupermarket_{autotrader,autoview,cap,cap_valuations,data,google,nexus_point}_ingestion/README.md` | 7 | Setup/boilerplate; no additional verified analytical definitions. |
| Intelligence Hub `README.md` | 1 | Local setup excluded from report knowledge. |
| Intelligence Hub `docs/2026-10-27_intel_hub_domain_move.md`, `ask_ai_status.md`, `improvement_plan.md` | 3 | Deployment and future engineering proposals not promoted to data facts. No AI dependency introduced. |
| Intelligence Hub `docs/self_analytics_report_format.md` | 1 | Existing saved-report contract preserved and extended. |
| Intelligence Hub `docs/wbr_report_discrepancies.md` | 1 | Historical observations separated from confirmed current metric decisions; do not recreate margin from an assumed recon cost. |
| Intelligence Hub `data/ai_knowledge_seed/{sales_performance,daily_summary}.md` | 2 | Source grain, metric meanings and date/snapshot cautions retained; existing seed entries preserved. |
| Intelligence Hub `data/wbr_report_glossary.md` | 1 | Historical glossary context; generic or conflicting example definitions not treated as verified universal formulas. |
| Intelligence Hub `tools/obeya_charts/README.md` | 1 | Source-gap guidance, without claiming an old snapshot proves current Snowflake absence. |
| `common_dbt_skills_packages/README.md` | 1 | Package setup, not analytical knowledge. |
| `common_dbt_skills_packages/skills/*/SKILL.md` | 7 | Read as documentation. Layer responsibilities, grain and validation principles retained; no skill/git workflow executed. |
| `common_dbt_skills_packages/skills/*/references/*.md` | 8 | `dbt-standards`, `sql-conventions`, `compute-table-standard`, compute/product/raw validation, business glossary and documentation conventions reviewed for source interpretation. Validation and glossary examples are not new UK facts. |
| `common_google_analytics_ingestion/README.md` | 1 | GA4-to-S3 source route, date overrides and lookback/cursor cautions. |
| `common_gsheet_to_s3/README.md` | 1 | Spreadsheet ingestion route, header normalization and raw string types. |
| `common_snowflake_to_gsheet/README.md` | 1 | Export route; not a new source of business facts. |
| `rundeck_power_bi_job_templates/README.md` | 1 | Administrative setup excluded. |
| `uk_dbt_{raw,transform,product}_data/README.md` | 3 | Technical setup; model SQL/YAML used for actual source contracts. |
| `uk_sharepoint_to_s3/readme.md`, `uk_snowflake_to_sharepoint/readme.md` | 2 | Ingestion/export possibilities, not proof of current workbook coverage. |

## HTML And Model Evidence

The supplied `buyer_estimates_vs_actual.html` was a saved SharePoint viewer.
The actual report was recovered from the iframe `srcdoc` in its companion
`buyer_estimates_vs_actual_files/b9b0c1f4-14f8-4ee2-bf13-d9bfba43f2a2.html`.
It informed multi-measure comparisons, per-chart matched cohorts, supplier/type
filters, weekly/monthly grouping and drill-down requirements. Its styling was
not copied.

Source fields were checked against `sales_performance__p.sql` and its YAML in
`uk_dbt_product_data/source_code/source/models/sales_performance`, and
`slt_kpis/daily_summary__p.sql`. These are repository checks, not live Snowflake
schema or data validation. The report source entries state that distinction.

Each new knowledge entry records its source. Proposed entries need website
review before they are used. Existing approved website knowledge remains the
editable authority; no user edits are overwritten by this bundle.
