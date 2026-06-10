"""CAP ID lookup by VRM (admin only)."""
import tempfile
import threading

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
import os
import time
import logging
from zoneinfo import ZoneInfo


from utils.s3_tools import download_file

from .core import (BASE_URL, templates, s3_bucketname, _track, get_current_user, _uk_now)

logger = logging.getLogger(__name__)

router = APIRouter()


_cap_lookup_progress: dict = {}
_cap_lookup_lock = threading.Lock()
_CAP_LOOKUP_S3_PREFIX = "applications/pricing/uk-vehicle-list-evaluator/{env}/cap-lookup/"

_CAP_LOOKUP_COLUMNS = [
    "VRM", "CAP ID", "Manufacturer", "Model", "Derivative", "Registration Date", "Note",
]
@router.get("/cap-lookup", response_class=HTMLResponse)
def cap_lookup_page(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    _track(request, 'page_view', '/cap-lookup')
    return templates.TemplateResponse("cap_lookup.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


@router.get("/cap-lookup/progress")
def cap_lookup_progress(request: Request, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    sid = request.session.get('session_id', '')
    with _cap_lookup_lock:
        p = _cap_lookup_progress.get(sid, {"pct": 0, "done": 0, "total": 0, "eta_s": None})
    return p


@router.post("/cap-lookup/process")
def cap_lookup_process(request: Request, payload: dict, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)

    from list_processor.cap_client import CAPClient
    from list_processor.structs import VehicleRow
    from concurrent.futures import ThreadPoolExecutor, as_completed

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

    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    ts_str = _uk_now().strftime("%Y%m%d_%H%M%S")
    filename = f"cap_lookup_{ts_str}.xlsx"
    s3_key = _CAP_LOOKUP_S3_PREFIX.format(env=env) + filename

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CAP Lookup"

    header_fill = PatternFill("solid", fgColor="BDD7EE")
    header_font = Font(bold=True)
    error_fill  = PatternFill("solid", fgColor="FFC7CE")
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    note_col = _CAP_LOOKUP_COLUMNS.index("Note") + 1

    ws.append(_CAP_LOOKUP_COLUMNS)
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
                ExtraArgs={"Metadata": {"vrm_count": str(len(vrms)), "processed_by": user}},
            )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return {"columns": _CAP_LOOKUP_COLUMNS, "rows": rows, "filename": filename}


@router.get("/cap-lookup/history")
def cap_lookup_history(user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
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
def cap_lookup_download(filename: str, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
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
def cap_lookup_load(filename: str, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
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
