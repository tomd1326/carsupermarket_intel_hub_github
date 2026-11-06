# Intelligence Hub improvement plan

Problems found while writing the Technical Guide (`/technical-guide`) on 2026-11-05,
from reading the code. Nothing here has been tested against the live site yet. Each
item gives where it is, why it matters, the fix, and how to check the fix.

Paths are under `source_code/router/backend/` unless they start with `terraform/`,
`db/`, `docs/` or a repo-root file.

Priority: **P1** fix first (security or money), **P2** fix soon (wrong data or
outages), **P3** tidy-up.

---

## P1 - Security and cost

### 1. Background job routes have no login or secret check
- **Where:** `routers/tasks.py:844` `/elaborate-task`, `tasks.py:742`
  `/api/fetch-supplier-stock-internal`, `tasks.py:439` and `:475`
  `/api/enterprise-email/create-task` and `/start-task`, `routers/reports.py:359`
  `/api/reports/run`. API Gateway has no authorizer (`terraform/prod/variables.tf:140`).
- **Why:** anyone who finds the address can start paid CAP and Autotrader runs,
  supplier website logins, report builds, or get an S3 upload link.
- **Fix:** add `check_worker_secret` (as `/keeper-change-check/run` already does) to
  `/elaborate-task`, `/api/fetch-supplier-stock-internal` and `/api/reports/run`, and
  pass the secret in `simulate_http`. For the two Power Automate routes, add a separate
  shared key held in Secrets Manager and in the Power Automate flow.
- **Test:** each route returns 403 without the secret; Upload, Get Stock, the Enterprise
  email flow and Apex Reports still complete on dev.

### 2. Session signing key is written in the code
- **Where:** `main.py:78`.
- **Why:** anyone with the repo can make a valid session cookie for any user, admin
  included.
- **Fix:** move the key to the `api_access` secret (new key name, e.g.
  `SESSION_SECRET_KEY`), read it at start-up, use a different value per environment.
  Changing it signs everyone out once.
- **Test:** login works on dev and prod; an old cookie is refused.

### 3. DVSA API login is written in the code
- **Where:** `utils/dvsa_client.py:19-21` (fallback values; Terraform never sets the
  env vars, so prod uses them). Also in git history.
- **Fix:** ask DVSA for a new client secret and API key, store them in `api_access`,
  remove the fallbacks.
- **Test:** MOT Check returns results on dev and prod; the old secret no longer works.

### 4. Password storage and login
- **Where:** `routers/auth.py:41`, `:64`, `:133`; `routers/settings.py:1791`, `:1876`,
  `:1918`; `utils/password_rules.py:10`; `routers/core.py:373-375`.
- **Why:** unsalted SHA-256 is quick to crack; the hash sits in the session cookie,
  which the browser can read; there is no limit on login attempts; the admin password
  hash is in the code and always accepted.
- **Fix:** hash with a slow salted method (bcrypt or argon2) and re-hash on next
  login; keep only a session ID in the cookie and compare the password change time
  instead of the hash; lock an account for some minutes after repeated failures;
  remove `_ADMIN_FALLBACK`. Raise the minimum length to 10.
- **Test:** existing users can still log in (and are re-hashed); five wrong passwords
  lock the account; the cookie no longer contains the hash.

### 5. Text put into pages without escaping
- **Where:** `templates/supplier_stock_evaluator.html:660-667` (file name, uploaded by,
  supplier), `templates/cap_lookup.html:372-373`, `templates/mot_check.html:251`,
  `:519-526`.
- **Why:** a file name containing HTML runs as script for whoever opens the page.
  Item 1 lets anyone set a file name.
- **Fix:** escape with one shared helper (move one of the ~12 copies of `esc()` into a
  shared JS file and use it everywhere).
- **Test:** upload a file named `<img src=x onerror=alert(1)>.csv`; it shows as text.

### 6. Cross-site requests
- **Where:** `main.py:78` (`same_site="none"`); no CSRF tokens; GET routes that change
  things: `tasks.py:323` `/delete-task/{id}`, `tasks.py:420` `/trigger-elaborate-task`.
- **Fix:** set `same_site="lax"` (the site and its API share one domain), make the two
  GET routes POST, remove `/trigger-elaborate-task` if unused.
- **Test:** login, Google OAuth callback and all popups still work.

### 7. AWS permissions too wide
- **Where:** `terraform/modules/iam/policies/aws_lambda_vehicle_list_evaluator_access_policy.json`:
  `s3:*`, `logs:*`, `lambda:InvokeFunction`, Secrets Manager and ECS all on `*`; dev
  can pass prod dbt roles. `utils/db_utils.py:25` uses the Aurora cluster admin login.
- **Fix:** limit S3 to `aa-raw-sources-uk`, Lambda invoke to the function itself,
  secrets to the five names used, ECS to the dbt task definitions; give dev its own
  policy. Ask the platform team for an app database user with rights only on
  `carsupermarket_pricer`.
- **Test:** every page and job works on dev after the change (needs the platform team).

### 8. Full error details shown to users
- **Where:** `exceptions.py:127`, `templates/error.html:57-61`; `/healthcheck`
  (`main.py:93-99`) returns the Lambda context to anyone.
- **Fix:** show the traceback to the admin only; return `{"status": "ok"}` from
  `/healthcheck`.

### 9. Other secrets
- CAP override password stored and shown as plain text (`routers/settings.py:275-279`,
  `templates/settings.html:432`) and copied to dev by Prod To Dev (`settings.py:628`).
  Encrypt it like the Google token, and never send it back to the page.
- Worker secret falls back to `AUTOTRADER_SECRET` (`routers/core.py:62-63`). Make
  `WORKER_SHARED_SECRET` required.
- The Nexus token is sent to an admin-editable host (`routers/api_requests.py:57-60`).
  Fix the host to `data-api.nexuspointapex.com`.

### 10. HPI Spec Check can bill twice
- **Where:** `routers/assess_vehicle.py:1298-1335`.
- **Why:** check-then-call with no lock; two clicks, or a failed save, pay again.
- **Fix:** insert a "pending" row for the VRM first (`INSERT ... ON CONFLICT DO
  NOTHING`) and only call HPI if the insert won; disable the button while waiting.

---

## P2 - Wrong data and reliability

### 11. Failed Autotrader calls cached for 24 hours
- **Where:** `utils/cache.py:121-130`.
- **Why:** a failed call returns `None`, which is stored and replayed; every read
  resets the expiry, so a popular entry never refreshes.
- **Fix:** do not store `None` or error results; do not reset the expiry on a read.
- **Test:** after an Autotrader error, the next lookup calls Autotrader again.

### 12. Dev and local touch production
- `utils/db_utils.py:96-124`: local runs write prod tables through
  `get_prod_pricer_connection`.
- `utils/db_utils.py:1877-1896`, `routers/settings.py:1748-1751`: the dev Lambda can
  change prod users and sessions.
- `routers/dbt_runs.py:111-116`: dev and local always start prod dbt.
- `routers/api_requests.py:67`: Nexus pulls default to the `v0` folder prod dbt reads.
- `source_code/router/docker-compose.yml:16`: `AWS_REGION=eu-west-1` makes the local
  container act as prod.
- **Fix:** decide for each one whether it is wanted (some are deliberate, e.g. one KPI
  layout everywhere); show a clear "This writes to PROD" label on the page where it
  is; change the compose file to `us-east-2`; default dev Nexus pulls to `dev`.

### 13. Requests with no timeout
- **Where:** `clients/autotrader_client.py:235`, `:266`, `:299`, `:489`;
  `utils/sharepoint_client.py:128`, `:153`, `:165`, `:186`, `:212`, `:367`, `:376`,
  `:403`, `:447`; `routers/buying_requirements.py:1541`.
- **Why:** a hung call holds a worker until the Lambda's 600 s limit.
- **Fix:** add `timeout=(5, 30)` to each.

### 14. Threads on Lambda
- **Where:** `routers/mot_check.py:89`, `routers/cars_bought.py:537`,
  `routers/pages.py:30-73` (home page warm-up), `routers/tasks.py:609-619`.
- **Why:** Lambda freezes the container once the response is sent, so the work can
  stop half way.
- **Fix:** move MOT Check and the Cars Bought job to the self-invoke pattern used by
  Keeper Change Check.

### 15. Database pool and logging
- `utils/db_utils.py:481-489`: a full pool raises `PoolError`, which nothing catches.
- `utils/app_log.py:175`: every warning takes a second connection, often while the
  request already holds one.
- `utils/db_utils.py:511-514`: connections go back to the pool without a rollback after
  an error.
- No RDS Proxy and no reserved concurrency: about 40 busy containers use up Aurora's
  connections.
- `main.py:30`: root log level DEBUG in production.
- **Fix:** roll back on return; catch `PoolError` with a clear message; write log rows
  in batches on their own small pool; set log level INFO; ask the platform team about
  RDS Proxy or set reserved concurrency.

### 16. Background jobs that fail silently or run twice
- **Where:** `utils/lambda_http.py:88-99`.
- **Why:** async Lambda calls are retried twice by AWS on error and have no failure
  destination.
- **Fix:** set the function's async retry count to 0 in Terraform
  (`aws_lambda_function_event_invoke_config`) and mark jobs that stop updating as
  failed (Keeper Change already does this after 20 minutes).

### 17. Slow pages hit the 29-second API Gateway limit
- **Where:** `routers/assess_vehicle.py:907-909`, `routers/cap_lookup.py:87` (CAP: 3
  tries x 20 s).
- **Fix:** use `api_limits.quick()` for any call a person is waiting on.

### 18. Same calculation in two places
- Ideal Stock / Required: `static/js/buying_requirements.js:1142-1180` and
  `routers/buying_requirements.py:1743-1765` use different filters, so the download can
  disagree with the page.
- Price bands: `buying_requirements.py:275` uses `<`, `utils/od_bands.py` uses `<=`, so
  a car at exactly 10,000 is in different bands on the two pages.
- MOT warnings in `routers/mot_check.py:206-290` and `templates/mot_check.html`.
- **Fix:** one calculation in Python, sent to the page; agree the band edge with Tom.

### 19. Dates in UTC
- **Where:** `routers/orders_deliveries.py:143`, `routers/reports.py:250`,
  `routers/kpi.py:657`, `:1937`, `:2200`, `routers/subsidy_rates.py:124`
  (`date.today()`).
- **Why:** Lambda runs in UTC, so in summer these are a day behind between midnight and
  01:00.
- **Fix:** use `_uk_now().date()`.

### 20. Caches shared between dev and prod
- **Where:** `.../shared/` S3 files (`orders_deliveries.py:166`, `trade_report.py:85`,
  `wbr_report_data.py:142`, `sharepoint_sources.py:66`).
- **Why:** dev code writing a changed format under the same version breaks prod.
- **Fix:** put the environment in the key, or raise the version in every change that
  alters the format.

### 21. S3 clients with no region
- **Where:** `routers/buying_requirements.py:507`, `:521`, `:534`, `:561`, `:651`,
  `:795`, `:804`; `utils/s3_tools.py:21`, `:35`.
- **Fix:** always pass `region_name='eu-west-1'`.

### 22. Settings changes only reach one container
- **Where:** CAP credential override (`clients/cap_client.py:62-79`,
  `routers/settings.py:283`).
- **Fix:** re-read the override on a short timer, like the other settings.

---

## P3 - Tidy-up

### 23. Admin is one hard-coded username
- `ADMIN_USERNAME = 'tdrayson'` (`routers/core.py:379`) plus about 35 literal
  `'tdrayson'` checks (`routers/settings.py`, `tasks.py:108`, `:244`, `:302`, `:667`,
  `orders_deliveries.py:463`, `db_utils.py:307`, `:312`) and 10 separate `_admin`
  helpers. Replace with an `admin` role in `user_roles` and one `require_admin`
  dependency.

### 24. Schema made by the app at run time
- About 60 `CREATE TABLE` / `ALTER TABLE` statements in 21 modules; many tables have no
  `db/` file; `db/00` and `db/01` cannot be re-run. `CREATE TABLE settings` runs on
  every settings save (`utils/db_utils.py:440`, `:467`). Move to numbered migration
  files run once at deploy.

### 25. Very large files
- `routers/kpi.py` 5,058 lines (also holds Google Ads and WBR), `static/js/kpi.js`
  8,952, `templates/settings.html` 2,645, `utils/db_utils.py` 2,355,
  `routers/settings.py` 2,168. Split by area, starting with Google Ads and WBR out of
  `kpi.py`.

### 26. Repeated page boilerplate and inline scripts
- No base template: the Bootstrap, Font Awesome and theme links are repeated in every
  page. Large inline scripts in `google_ads_campaigns.html`,
  `supplier_stock_evaluator.html`, `subsidy_rates.html`, `mot_check.html`,
  `keeper_change_check.html`, `settings.html`, `users.html`. Add a `_head.html`
  include and move scripts to `static/js/` so `node --check` can reach them.

### 27. Old browser libraries, no integrity checks
- Bootstrap 5.1.3, Font Awesome 6.0.0-beta3, SheetJS `xlsx@0.18.5` (known security
  advisories; newer versions are only on the SheetJS site) loaded from CDNs with no
  `integrity` attribute. Update, add `integrity`, or serve from `static/`.

### 28. Unused code
- `source_code/evaluator/` (still built by `.gitlab-ci.yml:80`, `:95`, `:111`), the
  evaluator ECR lookup in `terraform/prod/main.tf:100-107`, `RedisCache` in
  `utils/cache.py:22-39`, `templates/reports_landing.html`, `utils/cortex.py` and its
  callers and the `cortex_model` settings, unused routes (`/test-snowflake`,
  `/buying-requirements/data`, `/stream-dbs`, `/trigger-elaborate-task`,
  `/kpi/custom-data`), `terraform/modules/secret_manager` (broken, unused), the
  unused `variables` Lambda env var.

### 29. Rules broken in the UI
- `text-transform: uppercase` outside number plates: `static/css/assess_vehicle.css`,
  `google_ads_campaigns.html:92`, `reports.html:73`, `subsidy_rates.html:37`, `:66`,
  `:96`, `:107`, `keeper_change_check.html:55`, `mot_check.html:65`.
- Hard-coded colours in page `<style>` blocks instead of `theme.css` tokens
  (`subsidy_rates.html`, `stock_list.html`, `google_ads_campaigns.html`).
- Hard-coded values that should be settings: SharePoint links with the year in them
  (`buying_requirements.html:40`, `:47`), the GA4 event list (`kpi.py:47-53`), the
  supplier ID map (`tasks.py:147`), CAP workers default 20 in `tasks.py:121` against 25
  in Settings.

### 30. Build, docs and process
- No test or lint stage in `.gitlab-ci.yml`; add a `pytest` job.
- `README.md` says Python 3.9 and conda; rewrite from the Technical Guide's Running It
  Locally section.
- Dockerfile build stage sets `TZ=Europe/Paris` (`source_code/router/Dockerfile:12`).
  It does not reach the running image, which sets `Europe/London`; change it to London
  so the two match.
- `terraform/*/variables.tf:145`, `:151`: dev and prod domain descriptions are swapped.
- Stale comments: `utils/db_utils.py:103-105` (says dev cannot reach prod),
  `routers/page_registry.py:47-48`, `routers/assess_vehicle.py:1`, `routers/mot_check.py:1`.
- `alerts.js:14-16` formats times as `en-US`.
- No CloudWatch alarms. Add at least Lambda errors and API Gateway 5xx.
- Write tests for login, sessions, `require_page`, the batch processor and the worker
  routes.
