# Ask AI - Status And Future Requirements

Intelligence Hub. Last updated 2026-10-30.

## Current Status

### Ask AI is switched off (2026-10-30)
- The website must not use Snowflake Cortex or any other Snowflake AI function, so it never spends Snowflake AI credits; it runs plain queries only.
- Every Ask AI call still goes through `utils/cortex.py` (`cortex_complete`), which now raises `AIUnavailable` ("Ask AI is switched off until the Claude API is set up") without connecting to Snowflake. KPI, Orders & Deliveries and Draft With AI show that message.
- Update Available Models in Settings > AI is disabled and its route returns the same message.
- Ask AI comes back on the Claude API (Anthropic Console) once the key is available: replace the body of `cortex_complete` (and rename it), keeping the same callers.
- The code alone does not stop Snowflake AI use: the app's Snowflake role can still call Cortex. Only an admin removing `SNOWFLAKE.CORTEX_USER` from that role makes it impossible.

### Settings > AI
| Card | What it holds |
|---|---|
| Model | Model dropdown, Update Available Models (disabled while Ask AI is off) |
| Pages | Ask AI on/off for KPI (`kpi_ai_summary`) and Orders & Deliveries (`orders_deliveries_ai`, off by default) |
| Page Settings | Per page: instructions, answer length limit, temperature, messages remembered, knowledge size limit, Reset To Default, Preview What The AI Sees |
| Knowledge | Entries every Ask AI reads: Table Descriptions, Column Notes (table and column picked from live Snowflake), Instructions, Learned Facts, YAML, Documents. Search and filters, Approve, Switch On/Off, Delete, Draft With AI, Add Starting Entries. Warns when a page's entries go over its size limit |

### Storage
- Aurora (prod pricer DB): `ai_knowledge` (entries) and `ai_page_settings` (per-page settings). Both are created on first use and copied by Copy Prod To Dev. See `db/20_ai_knowledge.sql`.
- Only entries that are **Active and On** reach the model. Starting entries, AI drafts and anything Ask AI suggests remembering are saved as **Proposed** until approved.
- Starting entries (`data/ai_knowledge_seed/`): Business Rules, `SALES_PERFORMANCE` (from the dbt YAML), `DAILY_SUMMARY` (from the SQL, as it has no YAML - needs checking).

### Pages
- **KPI:** `/kpi/ai-summary` is admin only and switched by Settings > AI. It sends the page's instructions and knowledge. There is no Ask AI panel on the KPI page yet.
- **Orders & Deliveries:** the Ask AI panel works as before, using its instructions and limits from Settings > AI. Off by default; the panel is hidden and requests are refused while off.

### Code
| File | Purpose |
|---|---|
| `utils/cortex.py` | The one place Ask AI calls a model. Switched off: raises `AIUnavailable`, no Snowflake connection |
| `utils/ai_knowledge.py` | Entries, page settings and defaults, knowledge text builder with size limit |
| `routers/ai_knowledge.py` | Admin-only JSON API behind Settings > AI, Draft With AI |
| `static/js/ai_settings.js` | Page Settings and Knowledge cards |
| `tests/test_ai_knowledge.py` | 8 tests |

## Known Gaps
- The list of pages with an Ask AI (`AI_PAGES` in `utils/ai_knowledge.py`) stays in code, because each page needs its own code before it can be switched on.

## Future Requirements

### 1. KPI Ask AI Panel
- Ask AI button at the top of the KPI page, admin only.
- Free-text questions, e.g. "Why did GPU-M decrease last week", "Cars under £15k sold this month".
- Asks clarifying questions first when the meaning is not settled (sold = ordered or delivered, cancelled and credited in or out, which date, table or chart), with option buttons and a stated default.
- Answers as text, a table or a Chart.js chart.
- "Teach it by telling it": Ask AI offers to save a fact; it goes in as Proposed for approval (`propose_entry` already exists).

### 2. Getting The Numbers
Undecided - two options:
- **A. Guided question builder, no LLM.** Question types with dropdowns (metric, period, comparison, filters). Business terms in Settings become SQL filters. "Why did X change" uses fixed SQL: change per unit, split by supplier channel, supplier and sales type, ranked by impact, with a mix-shift check. Answers are tables, charts and template sentences. No model cost; numbers always exact; cannot understand free text.
- **B. Structured query request from the Claude API.** The model fills in a request (table, columns, filters, dates); the server checks every name against the allowed tables and columns before running it. The model only writes about the results, never does the arithmetic.
- Either way: numbers come from SQL, never from the model; personal data (names, emails, phones) never goes to the model; row-level results go straight to the browser.
- Questions to settle: which question types come first, and whether the model stays as an optional switch for writing the paragraph.

### 3. Knowledge
- Check and approve the three starting entries.
- Write descriptions for more tables (Draft With AI, then edit).
- Reading the dbt YAML from the site directly (not built; the site only sees Snowflake).

### 4. Usage And Cost
- A usage table in Settings > AI: calls, tokens and cost per model, last 7 and 30 days, from the Claude API's own usage figures.

### 5. Other Pages
- Add Ask AI to more pages one at a time; each gets its own Page Settings and knowledge scope.
- Orders & Deliveries: drill from an Ask AI finding into the View popup for individual vehicles.

### 6. Limits To Watch
- API Gateway stops requests at 29 seconds. Model calls that may run longer should be a background job the page polls (as Cars Bought's invoice reading does).
