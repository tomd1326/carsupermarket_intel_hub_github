"""CAP ID lookup by VRM, and single CAP valuations (current or historic) by VRM or CAP ID."""
import datetime
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
import os
import time
import logging
from zoneinfo import ZoneInfo


from utils.s3_tools import download_file

from .core import (BASE_URL, templates, s3_bucketname, _track, _uk_now,
                   require_page, require_page_api)

logger = logging.getLogger(__name__)

router = APIRouter()


_cap_lookup_progress: dict = {}
_cap_lookup_lock = threading.Lock()
_CAP_LOOKUP_S3_PREFIX = "applications/pricing/uk-vehicle-list-evaluator/{env}/cap-lookup/"

_CAP_LOOKUP_COLUMNS = [
    "VRM", "CAP ID", "Manufacturer", "Model", "Derivative", "Registration Date", "Note",
]
_CAP_VALUATION_COLUMNS = [
    "CAP ID", "Manufacturer", "Model", "Derivative", "Registration Date", "Mileage",
    "Monthly Clean", "Monthly Retail", "Live Clean", "Live Retail", "Note",
]


_CAP_HISTORIC_COLUMNS = [
    "CAP Month", "Live Value On", "CAP ID", "Manufacturer", "Model", "Derivative",
    "Registration Date", "Mileage",
    "Approx. CAP Monthly Retail", "Approx. CAP Monthly Clean", "Note",
]
# CAP publishes each monthly edition about 3 working days before the 1st; the
# live value on that day is used as the monthly figure, rounded as CAP's screen.
_CAP_RELEASE_WORKING_DAYS = 3
_CAP_MONTHLY_ROUND_TO = 50


@router.get("/cap-lookup", response_class=HTMLResponse)
def cap_lookup_page(request: Request, user: str = Depends(require_page('cap_lookup'))):
    _track(request, 'page_view', '/cap-lookup')
    return templates.TemplateResponse("cap_lookup.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


@router.get("/cap-lookup/progress")
def cap_lookup_progress(request: Request, user: str = Depends(require_page_api('cap_lookup'))):
    sid = request.session.get('session_id', '')
    with _cap_lookup_lock:
        p = _cap_lookup_progress.get(sid, {"pct": 0, "done": 0, "total": 0, "eta_s": None, "label": None})
    return p


@router.post("/cap-lookup/process")
def cap_lookup_process(request: Request, payload: dict, user: str = Depends(require_page_api('cap_lookup'))):
    from list_processor.cap_client import CAPClient
    from list_processor.structs import VehicleRow

    vrms = [v.strip().upper() for v in (payload.get("vrms") or []) if v.strip()]
    if not vrms:
        raise HTTPException(status_code=400, detail="No VRMs provided")

    sid = request.session.get('session_id', '')
    total = len(vrms)
    _track(request, 'cap_lookup', '/cap-lookup', {'vrm_count': total})
    with _cap_lookup_lock:
        _cap_lookup_progress[sid] = {"pct": 0, "done": 0, "total": total, "eta_s": None}

    cap = CAPClient()
    results = [None] * total
    start = time.time()
    done_count = 0

    def fetch(idx_vrm):
        idx, vrm = idx_vrm
        try:
            vehicle = VehicleRow(vrm=vrm, mileage=0)
            data = cap.vrm_valuation(vehicle)
            return idx, vrm, data, None
        except Exception as e:
            return idx, vrm, None, str(e)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch, (i, v)): i for i, v in enumerate(vrms)}
        for fut in as_completed(futures):
            idx, vrm, data, err = fut.result()
            results[idx] = (vrm, data, err)
            done_count += 1
            elapsed = time.time() - start
            avg = elapsed / done_count
            eta_s = (total - done_count) * avg
            with _cap_lookup_lock:
                _cap_lookup_progress[sid] = {
                    "pct": round(done_count / total * 100, 1),
                    "done": done_count,
                    "total": total,
                    "eta_s": round(eta_s, 1),
                }

    with _cap_lookup_lock:
        _cap_lookup_progress.pop(sid, None)

    rows = []
    for vrm, data, err in results:
        if err or not data:
            rows.append([vrm, "", "", "", "", "", err or "Error"])
        else:
            rows.append([
                vrm,
                data.get('capid') or "",
                data.get('capman') or "",
                data.get('capmod') or "",
                data.get('capder') or "",
                data.get('registered_date') or "",
                data.get('note') or "",
            ])

    filename = _save_xlsx(_CAP_LOOKUP_COLUMNS, rows, "cap_lookup", len(vrms), user)
    return {"columns": _CAP_LOOKUP_COLUMNS, "rows": rows, "filename": filename}


def _save_xlsx(columns: list, rows: list, prefix: str, count: int, user: str) -> str:
    """Build the results workbook, upload it to S3 and return its filename."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from utils.file_handler import DEFAULT_FONT_NAME, set_default_font

    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    ts_str = _uk_now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{ts_str}.xlsx"
    s3_key = _CAP_LOOKUP_S3_PREFIX.format(env=env) + filename

    wb = openpyxl.Workbook()
    set_default_font(wb)
    ws = wb.active
    ws.title = "CAP Lookup"

    header_fill = PatternFill("solid", fgColor="BDD7EE")
    header_font = Font(name=DEFAULT_FONT_NAME, bold=True)
    error_fill  = PatternFill("solid", fgColor="FFC7CE")
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    note_col = columns.index("Note") + 1

    ws.append(columns)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
        cell.border = border

    for row in rows:
        ws.append(row)
        row_idx = ws.max_row
        for col_idx, cell in enumerate(ws[row_idx], start=1):
            cell.border = border
            if col_idx == note_col and cell.value:
                cell.fill = error_fill

    ws.freeze_panes = "A2"
    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 2, 10), 40)

    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        import boto3 as _boto3
        wb.save(tmp_path)
        with open(tmp_path, 'rb') as f:
            _boto3.client('s3').upload_fileobj(
                f, s3_bucketname, s3_key,
                ExtraArgs={"Metadata": {"vrm_count": str(count), "processed_by": user}},
            )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return filename


def _capid_and_reg_date(payload: dict) -> tuple:
    """CAP ID and registration date (dd/mm/yyyy) from the form, rejected with a clear message if invalid."""
    try:
        capid = int(str(payload.get('capid') or '').strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="CAP ID must be a number")
    reg_text = str(payload.get('registration_date') or '').strip()
    try:
        reg_date = datetime.datetime.strptime(reg_text, '%d/%m/%Y').date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Registration date must be dd/mm/yyyy")
    return capid, reg_date, reg_text


def _cap_description(cap, capid: int, reg_date: datetime.date, mileage: int) -> dict:
    """
    Make, model, derivative and current monthly values from CAPIDValuation.
    Never raises: a failure is returned as 'note' so the valuations still show.
    """
    from list_processor.structs import VehicleRow

    out = {'make': "", 'model': "", 'derivative': "", 'clean': None, 'retail': None, 'note': ""}
    try:
        vehicle = VehicleRow(vrm='', mileage=mileage, capid=capid, registration_date=reg_date)
        monthly = cap.capid_monthly_valuation(vehicle)
    except Exception as e:
        logger.warning("CAPIDValuation failed for %s: %s", capid, e)
        out['note'] = f"Make/model lookup failed: {e}"
        return out
    if not monthly:
        out['note'] = "CAP returned no details for this CAP ID and registration date"
        return out
    out.update(make=monthly.get('CAPMan') or "", model=monthly.get('CAPMod') or "",
               derivative=monthly.get('CAPDer') or "",
               clean=monthly.get('Clean'), retail=monthly.get('Retail'))
    return out


def _historic_valuation(request: Request, payload: dict, mileage: int, user: str) -> dict:
    """
    Approximate CAP monthly values for a CAP ID, from N months back to the latest released edition.

    We do not subscribe to CAP's Used Values product, so each month's edition is
    rebuilt from the live endpoint on its release date: 3 working days before the
    1st (weekends skipped, bank holidays not). Checked against CAP's own Historic
    Valuation screen for CAP ID 97326: 9 of 13 months matched exactly at mileage
    rounded to 1,000 and values rounded to 50; the rest were 100-150 out, most
    likely where the real release day differed.
    """
    from list_processor.cap_client import CAPClient

    capid, reg_date, reg_text = _capid_and_reg_date(payload)
    try:
        months_back = int(payload.get('months_back', 12))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Months back must be a whole number")
    if not 0 <= months_back <= 60:
        raise HTTPException(status_code=400, detail="Months back must be 0 to 60")

    _track(request, 'cap_valuation', '/cap-lookup', {'mode': 'historic', 'capid': capid})

    today = _uk_now().date()
    editions = []
    for offset in range(-months_back, 1):
        idx = today.year * 12 + (today.month - 1) + offset
        editions.append(datetime.date(idx // 12, idx % 12 + 1, 1))

    def release_date(edition):
        d, working = edition, 0
        while working < _CAP_RELEASE_WORKING_DAYS:
            d -= datetime.timedelta(days=1)
            if d.weekday() < 5:
                working += 1
        return d

    # Future dates do not work on the live endpoint, so only editions already released.
    releases = {e: release_date(e) for e in editions}
    editions = [e for e in editions if releases[e] <= today]

    cap = CAPClient()
    sid = request.session.get('session_id', '')
    total = len(editions)
    start = time.time()
    with _cap_lookup_lock:
        _cap_lookup_progress[sid] = {"pct": 0, "done": 0, "total": total, "eta_s": None,
                                     "label": f"Starting: {total} monthly values to check"}

    def fetch(edition):
        try:
            vals = cap.live_valuation_on(capid, reg_date, mileage, releases[edition], round_to=_CAP_MONTHLY_ROUND_TO)
            return edition, vals, None
        except Exception as e:
            return edition, None, str(e)

    results = {}
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            # The live endpoint returns values only, so make/model come from one
            # CAPIDValuation call, run alongside the monthly checks.
            describe = pool.submit(_cap_description, cap, capid, reg_date, mileage)
            futures = [pool.submit(fetch, e) for e in editions]
            for done_count, fut in enumerate(as_completed(futures), start=1):
                edition, vals, err = fut.result()
                results[edition] = (vals, err)
                avg = (time.time() - start) / done_count
                with _cap_lookup_lock:
                    _cap_lookup_progress[sid] = {
                        "pct": round(done_count / total * 100, 1),
                        "done": done_count,
                        "total": total,
                        "eta_s": round((total - done_count) * avg, 1),
                        "label": f"Checked {edition.strftime('%m/%Y')} ({done_count} of {total})",
                    }
            desc = describe.result()
    finally:
        with _cap_lookup_lock:
            _cap_lookup_progress.pop(sid, None)

    rows = []
    for edition in editions:
        vals, err = results[edition]
        v = vals or ()
        rows.append([
            edition.strftime('%m/%Y'), releases[edition].strftime('%d/%m/%Y'), capid,
            desc['make'], desc['model'], desc['derivative'],
            reg_text, mileage,
            v[0] if v else "", v[1] if v else "",
            "; ".join(n for n in (err, desc['note']) if n),
        ])

    filename = _save_xlsx(_CAP_HISTORIC_COLUMNS, rows, "cap_valuation", 1, user)
    return {"columns": _CAP_HISTORIC_COLUMNS, "rows": rows, "filename": filename}


@router.post("/cap-lookup/valuation")
def cap_lookup_valuation(request: Request, payload: dict, user: str = Depends(require_page_api('cap_lookup'))):
    """One CAP valuation, either by VRM + mileage or by CAP ID + registration date + mileage."""
    from list_processor.cap_client import CAPClient
    from list_processor.structs import VehicleRow
    from .assess_vehicle import _cap_lookup

    mode = payload.get('mode')
    try:
        mileage = int(float(str(payload.get('mileage') or 0)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Mileage must be a number")
    if mileage <= 0:
        raise HTTPException(status_code=400, detail="Mileage is required")

    if mode == 'historic':
        return _historic_valuation(request, payload, mileage, user)

    vrm = ''
    if mode == 'vrm':
        vrm = str(payload.get('vrm') or '').strip().upper().replace(' ', '')
        if not vrm:
            raise HTTPException(status_code=400, detail="VRM is required")
        _track(request, 'cap_valuation', '/cap-lookup', {'mode': mode, 'vrm': vrm})
        try:
            d = _cap_lookup(vrm, mileage)
        except Exception as e:
            logger.error("CAP valuation by VRM failed for %s: %s", vrm, e)
            raise HTTPException(status_code=502, detail=f"CAP lookup failed: {e}")
    elif mode == 'capid':
        capid, reg_date, reg_text = _capid_and_reg_date(payload)
        _track(request, 'cap_valuation', '/cap-lookup', {'mode': mode, 'capid': capid})

        cap = CAPClient()
        desc = _cap_description(cap, capid, reg_date, mileage)
        d = {
            'cap_id': capid, 'make': desc['make'], 'model': desc['model'],
            'derivative': desc['derivative'], 'registered': reg_text,
            'monthly_clean': desc['clean'], 'monthly_retail': desc['retail'],
            'live_clean': None, 'live_retail': None, 'note': desc['note'] or None,
        }
        try:
            vehicle = VehicleRow(vrm='', mileage=mileage, capid=capid, registration_date=reg_date)
            live_retail, live_clean = cap.live_valuation(vehicle)
            d['live_retail'] = live_retail
            d['live_clean'] = live_clean
        except Exception as e:
            d['note'] = d['note'] or f"Live valuation failed: {e}"
    else:
        raise HTTPException(status_code=400, detail="Unknown valuation mode")

    row = [
        d.get('cap_id') or "", d.get('make') or "", d.get('model') or "", d.get('derivative') or "",
        d.get('registered') or "", mileage,
        d.get('monthly_clean') if d.get('monthly_clean') is not None else "",
        d.get('monthly_retail') if d.get('monthly_retail') is not None else "",
        d.get('live_clean') if d.get('live_clean') is not None else "",
        d.get('live_retail') if d.get('live_retail') is not None else "",
        d.get('note') or "",
    ]
    rows = [row]
    filename = _save_xlsx(_CAP_VALUATION_COLUMNS, rows, "cap_valuation", 1, user)
    return {"columns": _CAP_VALUATION_COLUMNS, "rows": rows, "filename": filename}


@router.get("/cap-lookup/history")
def cap_lookup_history(user: str = Depends(require_page_api('cap_lookup'))):
    import boto3
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    prefix = _CAP_LOOKUP_S3_PREFIX.format(env=env)
    try:
        resp = boto3.client('s3').list_objects_v2(Bucket=s3_bucketname, Prefix=prefix)
        objects = sorted(resp.get('Contents', []), key=lambda o: o['LastModified'], reverse=True)
        s3 = boto3.client('s3')

        def _meta(key):
            try:
                m = s3.head_object(Bucket=s3_bucketname, Key=key).get('Metadata', {})
                return {"vrm_count": int(m['vrm_count']) if m.get('vrm_count') else None, "processed_by": m.get('processed_by')}
            except Exception:
                return {"vrm_count": None, "processed_by": None}

        result = []
        for o in objects:
            if o['Key'] == prefix:
                continue
            meta = _meta(o['Key'])
            result.append({
                "filename":     o['Key'].replace(prefix, ''),
                "ts":           o['LastModified'].astimezone(ZoneInfo('Europe/London')).strftime("%d/%m/%Y %H:%M"),
                "size_kb":      round(o['Size'] / 1024, 1),
                "vrm_count":    meta["vrm_count"],
                "processed_by": meta["processed_by"],
            })
        return result
    except Exception as e:
        logger.warning("CAP lookup history S3 list failed: %s", e)
        return []


@router.get("/cap-lookup/download/{filename}")
def cap_lookup_download(filename: str, user: str = Depends(require_page_api('cap_lookup'))):
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    s3_key = _CAP_LOOKUP_S3_PREFIX.format(env=env) + filename
    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        download_file(tmp_path, s3_bucketname, s3_key)
        return FileResponse(
            tmp_path, filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=404, detail="File not found in S3")


@router.get("/cap-lookup/load/{filename}")
def cap_lookup_load(filename: str, user: str = Depends(require_page_api('cap_lookup'))):
    import openpyxl
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    s3_key = _CAP_LOOKUP_S3_PREFIX.format(env=env) + filename
    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        download_file(tmp_path, s3_bucketname, s3_key)
        wb = openpyxl.load_workbook(tmp_path, read_only=True, data_only=True)
        ws = wb.active
        rows_iter = iter(ws.iter_rows(values_only=True))
        header = next(rows_iter, None)
        if not header:
            return {"columns": [], "rows": []}
        columns = [str(c) if c is not None else "" for c in header]
        rows = [[str(c) if c is not None else "" for c in row] for row in rows_iter]
        wb.close()
        return {"columns": columns, "rows": rows}
    except Exception:
        raise HTTPException(status_code=404, detail="File not found in S3")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
