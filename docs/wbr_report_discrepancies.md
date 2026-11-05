# Weekly Business Review Report: differences from the Sheet

Compared 2026-11-05, review date 2026-11-01 (months 2026-08 to 2026-11, weeks W40 to W44), against
the live Sheet "UK - Weekly Business Review v6". Every cell was checked by script, on the tabs
`1- EBIT landing`, `3&5`, `4 - Inventories EoP`, `6- Marketing Dynamism`, `GPU MS by Channel`,
`Charts Finance`, `Charts GPU-M`, `Charts GPU-S`, `Ctrl - breakdown GPU MS` and `graphWeek-setup`.

**Tom's decisions of 2026-11-05, included in this comparison.** It was run again with them the same day,
using the Sheet values read earlier that day, so the only changes are from the report's side:

- **GPU-M, GPU-MS and GPU-MSB are Snowflake's `gpu_m`, `gpu_ms` and `gpu_msb`.** The £495 recon cost for a car
  with none or under £100 was a guess and is gone. GPU-S is `gpu_s`, within £1 of the old sum on all 20,469
  cars delivered since 2025-12. Section 1 shows what this changed against the Sheet.
- **Apex holds a car's costs as one number**, so the recon and other split still comes from AutoView. A new
  breakdown row, `Apex Vs AutoView Difference`, holds what they do not account for: -£8 to +£20 per car a
  month from 2025-12, on 8% to 23% of cars. 2025-11 is -£472, on 97%, as AutoView recon barely exists then,
  so AutoView Costs Start now defaults to 2025-12.
- **One cause of that difference:** 465 AutoView records sit on two or more Apex stock ids (944 of 20,466
  rows delivered since 2025-12), so their AutoView costs are on every one of those rows.
- **The Report's charts are now KPI charts**, chosen per section on the page, so charts are no longer in this
  comparison: 1,549 cells, against 1,993 when they were.

For this comparison only, `Buy back` sales were put back into `C2B2C - Other`, as the Sheet's rows
still have them there (see section 2).

## 1. Result

**1,030 of 1,549 cells match within the Sheet's rounding (66.5%). A further 260 are recent rows with a
median gap of 1.2%. No cell is blank.**

| What | Cells | Share | Median gap |
|---|---:|---:|---:|
| Match | 1,030 | 66.5% | |
| Recent rows changed since the Sheet's refresh (section 3) | 260 | 16.8% | 1.2% |
| Current month, only one day old (section 3) | 68 | 4.4% | 5.1% |
| The Sheet's YTD columns are broken (section 4) | 61 | 3.9% | 28.7% |
| Accounts moved since the Sheet copied them (section 3) | 45 | 2.9% | 2.4% |
| Vehicles Ordered GPU-M, not explained (section 5) | 40 | 2.6% | 7.2% |
| Older frozen rows, restated costs (section 3) | 36 | 2.3% | 0.4% |
| Top 10 "before" column starts later (section 2) | 9 | 0.6% | 13.1% |

**What the new GPU-M changed.** Of the 1,485 different cells compared in both runs, 979 matched before and
966 match now: 18 stopped matching and 5 started. The Sheet still uses the old way, so these rows show the gap:

| Period | GPU-M Sheet | GPU-M now | GPU-M before | GPU-MSB Sheet | GPU-MSB now |
|---|---:|---:|---:|---:|---:|
| 2026-08 | 699 | 734 | 700 | 1,470 | 1,505 |
| 2026-09 | 705 | 719 | 702 | 1,439 | 1,453 |
| 2026-10 | 739 | 739 | 731 | 1,513 | 1,491 |
| 2026-11 | 810 | 798 | 794 | 1,416 | 1,395 |
| W40 | 752 | 765 | 749 | 1,457 | 1,463 |
| W41 | 755 | 750 | 747 | 1,522 | 1,496 |
| W42 | 799 | 803 | 790 | 1,563 | 1,541 |
| W43 | 692 | 696 | 693 | 1,499 | 1,484 |
| W44 | 698 | 695 | 685 | 1,476 | 1,453 |

**Match in full:** stock B2C, sleeping stock, rotation refurb, recon, the five enquiry rows and their
trend arrows, advertised GPU-M.

**Budget:** all 924 figures from 2025-10 onwards match the Sheet's Budget tab. Earlier months were
never imported and are not used.

**Both "Should Be Nil" check rows are 0.000 in every period.** The Sheet's own check shows 87 to 267.

## 2. Where the report differs on purpose

| Item | Report | Sheet | Why |
|---|---|---|---|
| GPU-M | Snowflake's `gpu_m`, the same figure as the KPI charts | AutoView recon and other costs plus standard lines, in the Power Query | Tom's decision. One central figure, and the £495 guess is gone |
| N-1 using AutoView costs, before 2025-12 | Blank | Filled from frozen rows | `SALES_AUTOVIEW` has almost no recon cost before then. AutoView Costs Start setting |
| Recon N-1 before the file starts | Blank | 0, or a part month (2025-10: 290) | The file starts 2025-10-13 |
| Inventories and days of inventories N-1 | Blank | Empty, 0 or `#N/A` | The Sheet's formula points at a row that does not exist |
| Top 10 "before" column | From 2025-12 | All frozen rows | Follows AutoView Costs Start |
| Deliveries after the review Sunday | Not counted | Counted if the Sheet is refreshed later | Nov 1: 24 against 26 |
| Purchases chart | `sales_channel = 'B2C'` | Fixed since item 5 of the plan was written | |
| `Buy back` channel | Own row | Inside `C2B2C - Other` (sales), left out of totals (purchases) | Uncommitted change in `suppliers__c.sql`, already in PROD. Sheet's purchases total W41 470, chart 480: the 10 is Buy Back |
| Current month's weekly average on `volume_trends` | Divides by the weeks so far | No such formula | 4.2 would understate a part month |
| `Net Deliveries B2C #` | Divides by 30/7 | Slide and chart use 30/7 | Plan section 5.5 said 4.2 |
| Monthly Gross and Net Orders charts | Blank for the review's month | Estimate multiplied up | A part month would read as a low forecast |
| GPU-S table | Admin fees are a memo row; groups 10, 20 and 27 added | Admin fees in the stack; those groups missing | Lines now add up to `gpu_s` |
| Breakdown | Adds Other Costs, Credited Deals, Valet, Write Downs, EV Charger, Other Products | Missing, or `#REF!` | Lines now add up |
| Finance company split | Top 5 plus Other | All Other (list is blank) | Oct 2026: MotoNovo 61%, Black Horse 16% |
| Week numbers | Real Monday to Sunday dates | `WEEKNUM(date-1)` | Can differ around New Year |
| Delivery Costs £/U (slide 4) | Left out | No source | |
| Charts | KPI page's charts: month and week drawn separately, red and green background, axis from the data | One chart with a gap, budget band, fixed Floor and Sky | Tom's choice, 2026-11-05 |
| Monthly cost charts (COCA, Sales, HQ + Sales) | Shown as positive, lower is better | Negative | The red and green background only reads a target above nil |
| Top 10 & Flop 10, KPI quality, cash | Not built | | Removed or out of scope |

## 3. Differences from timing

**The Sheet's rows are frozen when pasted. Snowflake keeps changing.**

- **Older months, confirmed.** The April and May GPU-M gap is one line: late costs. Finance's table now
  says -166.48 and -167.66; the Sheet's rows hold -207 and -212. With the new GPU-M, Apr is 881 against
  817 and May 949 against 896; late costs explain about 40 of each. June's gap was AutoView recon cost:
  -560 against -541.
- **October standards, confirmed for late costs.** The Sheet's Oct late cost (-223) is September's
  figure, so it was copied before October's was published (the model carries the last month forward).
  Snowflake now has -218.
- **October GPU-S, cause likely.** About £20 lower than the Sheet in Oct and W41 to W44 (732 against
  752), while Aug and Sep agree. Standard GPU-S costs are -118 here against about -97 implied by the
  Sheet. Product and financing lines agree.
- **Net Orders, cause likely.** W44 437 against 461, W40 to W43 within a few. Probably later
  cancellations. Not checked order by order.
- **Accounts.** The Sheet's `data_accounts` is copied by hand. Sep 2026 is 1,807 units here against
  1,800. Oct GPU-M is 726 here against 740, so Oct EBIT is 46 against 71. Closed months that agree
  also agree on the formulas: Aug EBIT 88, EBITDA 323, Overheads -1,822.
- **Current month.** One day of deliveries, so small changes move the figures a lot.

## 4. Where the Sheet is wrong

| Item | Sheet | Report |
|---|---|---|
| YTD columns on `1- EBIT landing` | `J13` -1,197, and units 3,436 = Oct 2025 + Nov 2026 | Financial year from October, setting. Oct + Nov: 3,587 units |
| Current month overheads | -1,884: Sales + HQ + IFRS only, unlike its other columns | Copied, so the sensitivity table ties out. But that table is titled EBIT and ties to EBITDA |
| Budget EBIT and EBITDA | Top block 298 and 513, lower block -12 and 230 | Budget lines: 274 and 484 |
| `graphWeek-setup` Purchases | Filter on the wrong column, in the older version | `sales_channel` |
| Web Leads into orders | Cancelled / Classifieds | Not built |
| Marketing cost per order | `#DIV/0!` | Not built |
| `Ctrl` check row | `#REF!` rows | Nil |
| Net Deliveries, GPU-MSB, GPU-M, GPU-S N-1 for 2025-10 | `#N/A` | From the accounts |

**Trend column:** the Sheet compares last week with the week before; the slide's footnote says the
average of the last 4 months. The report follows the Sheet, and Settings > Report can switch it.

**Budget rounding:** the imported Budget holds rounded figures, so Days of inventories for Oct is 40
and the inventories budget 2,440, against 2,434 in the Sheet.

## 5. Not explained

**Vehicles Ordered GPU-M (40 cells, none matching, in "More" on `volume_trends`).** The figures here were
looked at before the new GPU-M: Sep was close (739 against 742), but weeks were not: W41 810 against 753.
Leaving out cancelled orders did not fix it (796). The Sheet's title says it excludes cancelled; its monthly
formula does not. Cancelled orders have no costs yet, so their figures keep moving, and the Sheet's are
frozen. This is likely but not confirmed. The three GPU-M waterfall charts it fed have been removed.

## 6. Not checked

- The page in a browser, by anyone.
- Same-day comparison. The Sheet was refreshed some days before this. A comparison run straight after
  a refresh would show how much of section 3 is timing.
- The cars bought, recon and net order rows of `average_volumes`: the Sheet's `6- Marketing Dynamism`
  only holds the enquiry rows. Recon and net orders are checked on `3&5`; cars bought is not checked.
- `Charts GPU-M` weeks at supplier level beyond a sample, which depend on the costs in section 3.
- `graphMTD-setup` and `graphWeek-setup`: charts are KPI charts now, so these are no longer compared. Six of
  the KPI charts' figures were checked against Snowflake directly for October, not against the Sheet.
- The sensitivity table and the rotation charts.
