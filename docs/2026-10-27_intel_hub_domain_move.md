# Intelligence Hub - 2026-10-27 session

## 1. "Save failed: wbr_notes must be a number" (KPI / Weekly Business Review settings)

**Cause:** the page was loaded before the `wbr_notes` setting existed on the server, so the
browser's copy of the settings had no `wbr_notes` and the save sent `null`.

- `/kpi/settings` (`routers/kpi.py`, `kpi_settings_save`) checks every key in
  `KPI_SETTING_LIMITS` with `int(raw.get(name))`. A missing key gives `int(None)`, which
  fails with "<name> must be a number".
- Both save paths in `static/js/kpi.js` (`saveSettings`, `saveWbrSettings`) post
  `{ ...settings }`, and `settings` is only fetched once, when the page loads (`/kpi/layout`).
- **Workaround:** hard refresh the page (Ctrl+F5) and save again.

**Planned fix (NOT applied yet):** in `kpi_settings_save`
- read `raw.get(name, KPI_DEFAULT_SETTINGS[name])` so a setting the page does not know about
  keeps its default instead of failing the whole save; still reject a value that is present
  but not a number
- colour settings: fall back to `KPI_COLOUR_SETTINGS[name]` when the key is missing
- the cross-checks (`days_window` / `days_total`, `days_after`, `wbr_*_before` / `wbr_*_scroll`)
  read from the `settings` dict built by `_kpi_settings(raw)` instead of `int(raw[...])`

## 2. Dev Terraform apply (domain move)

The dev pipeline (`dev` at `b71fce82`) applied successfully:
`Apply complete! Resources: 9 added, 1 changed, 9 destroyed.`

- `dev-uk-vle.aramis-group.eu` was removed (DNS records, hosted zone, certificate, domain
  mapping) and `dev-uk-intel-hub.aramis-group.eu` was created in its place.
- The first `terraform init` error ("Failed to read file ... remote_backend.conf") is
  harmless: the second `init` configures the S3 backend fine.
- The `dynamodb_table` deprecation warning is only a warning.

## 3. Moving prod to uk-intel-hub.aramis-group.eu

Merging `dev` into `main` brings:
- `terraform/prod/variables.tf`: prod domain `uk-vle.aramis-group.eu` -> `uk-intel-hub.aramis-group.eu`
- `terraform/modules/api_gateway/api.tf`: an `ANY` method on the root resource, so the bare
  address reaches the app instead of "Missing Authentication Token"

The prod Terraform apply replaces the old domain rather than adding a second one, so
**`https://uk-vle.aramis-group.eu` stops working**. Tom decided to drop it rather than keep it.
`.gitlab-ci.yml` runs the jobs for both `main` and `dev`. Expect a few minutes of downtime
while the new DNS and certificate come up (about 2m19s for the certificate on dev).
Alex needs to do the same step for prod that he did for dev.

## 4. /dev removed from the URLs (decided: yes, for prod and local too)

Changes made:
- `source_code/router/backend/routers/core.py`: `BASE_URL = os.getenv("BASE_URL", "")` for
  every environment (it used to be `""` on dev only and `/dev` everywhere else).
- `source_code/router/backend/main.py` (already in place): old `/dev/...` GET links redirect
  (301) to the same page without `/dev`; `Mangum(..., api_gateway_base_path=BASE_URL or "/")`.
- `source_code/router/backend/utils/db_utils.py`: password-link defaults are now
  `https://uk-intel-hub.aramis-group.eu` (prod) and `https://dev-uk-intel-hub.aramis-group.eu` (dev).
- `C:\Git\.claude\carsupermarket_vehicle_list_evaluator.md`: the local dev server is now
  `http://127.0.0.1:8080/` with no `/dev`; the prod and dev URLs are updated.

Done by Tom:
- Settings > Users > Password Links changed to the new addresses (the saved values
  override the code defaults).
- Dev site checked and working.

## After merging to main - check
- `https://uk-intel-hub.aramis-group.eu/` sends you to the login page.
- An old `/dev/login` link redirects to `/login`.
- If it fails: DNS or the certificate is still coming up, or the prod Terraform apply did not run.
