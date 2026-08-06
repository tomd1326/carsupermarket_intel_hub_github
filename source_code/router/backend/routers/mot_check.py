"""MOT check via DVSA API (admin only)."""
import tempfile
import threading

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
import json
import os
import time
import logging
import datetime


from utils.s3_tools import download_file
from utils.db_utils import (get_database_connection, get_settings)

from .core import (BASE_URL, templates, s3_bucketname, _track, get_current_user, _uk_now)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── MOT Check (admin only) ────────────────────────────────────────────────────
_mot_progress: dict = {}   # keyed by session_id: {pct, done, total, eta_s}
_mot_lock = threading.Lock()
_MOT_S3_PREFIX = "applications/pricing/uk-vehicle-list-evaluator/{env}/mot-check/"
@router.get("/mot-check", response_class=HTMLResponse)
def mot_check_page(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    _track(request, 'page_view', '/mot-check')
    return templates.TemplateResponse("mot_check.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


@router.get("/mot-check/progress")
def mot_check_progress(request: Request, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    sid = request.session.get('session_id', '')
    with _mot_lock:
        p = _mot_progress.get(sid)
    if p is not None:
        return p
    # Fall back to S3 completion marker (poll may hit a different Lambda instance)
    try:
        import boto3 as _boto3
        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        s3_key = _MOT_S3_PREFIX.format(env=env) + f"progress_{sid}.json"
        obj = _boto3.client('s3', region_name='eu-west-1').get_object(Bucket=s3_bucketname, Key=s3_key)
        return json.loads(obj['Body'].read())
    except Exception:
        pass
    return {"pct": 0, "done": 0, "total": 0, "eta_s": None, "complete": False, "filename": None, "error": None}


@router.post("/mot-check/process")
def mot_check_process(request: Request, payload: dict, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)

    vrms = [v.strip().upper() for v in (payload.get("vrms") or []) if v.strip()]
    if not vrms:
        raise HTTPException(status_code=400, detail="No VRMs provided")

    sid = request.session.get('session_id', '')
    total = len(vrms)
    _track(request, 'mot_check', '/mot-check', {'vrm_count': total})

    with get_database_connection() as conn:
        settings = get_settings(conn, user=user)
    mot_workers = int(settings.get('mot_workers', 10))

    initial_state = {"pct": 0, "done": 0, "total": total, "eta_s": None, "complete": False, "filename": None, "error": None}
    with _mot_lock:
        _mot_progress[sid] = initial_state

    t = threading.Thread(target=_mot_process_background, args=(sid, vrms, user, mot_workers), daemon=True)
    t.start()

    return {"queued": True, "total": total}


def _mot_process_background(sid: str, vrms: list, user: str, mot_workers: int):
    from utils.dvsa_client import DVSAClient, NOT_FOUND as DVSA_NOT_FOUND
    from concurrent.futures import ThreadPoolExecutor, as_completed

    total = len(vrms)
    results = [None] * total
    start = time.time()
    done_count = 0
    errored_indices = {}
    _last_s3_write = [0.0]  # mutable for closure

    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    s3_progress_key = _MOT_S3_PREFIX.format(env=env) + f"progress_{sid}.json"

    def _write_progress_to_s3(state: dict):
        try:
            import boto3 as _boto3
            _boto3.client('s3', region_name='eu-west-1').put_object(
                Bucket=s3_bucketname, Key=s3_progress_key,
                Body=json.dumps(state).encode(), ContentType='application/json',
            )
        except Exception as _e:
            logger.warning("[DVSA] S3 progress write failed: %s", _e)

    # Write initial state to S3 as first thing in the thread — cold-start poll instances
    # need to see total > 0 from their first poll, before any DVSA requests complete.
    with _mot_lock:
        _initial = _mot_progress.get(sid, {"pct": 0, "done": 0, "total": total, "eta_s": None, "complete": False, "filename": None, "error": None})
    threading.Thread(target=_write_progress_to_s3, args=(_initial,), daemon=True).start()

    client = DVSAClient()

    def fetch(idx_vrm):
        idx, vrm = idx_vrm
        try:
            return idx, vrm, client.get_vehicle_by_vrm(vrm), None
        except Exception as e:
            logger.error("[DVSA] %s fetch error: %s", vrm, e)
            return idx, vrm, None, str(e)

    try:
        with ThreadPoolExecutor(max_workers=mot_workers) as pool:
            futures = {pool.submit(fetch, (i, v)): i for i, v in enumerate(vrms)}
            for fut in as_completed(futures):
                idx, vrm, vehicle_json, err = fut.result()
                not_found = vehicle_json is DVSA_NOT_FOUND
                results[idx] = _build_mot_row(vrm, None if not_found else vehicle_json, not_found=not_found)
                if err:
                    errored_indices[idx] = vrm
                done_count += 1
                elapsed = time.time() - start
                avg = elapsed / done_count
                eta_s = (total - done_count) * avg
                state = {
                    "pct": round(done_count / total * 100, 1),
                    "done": done_count,
                    "total": total,
                    "eta_s": round(eta_s, 1),
                    "complete": False,
                    "filename": None,
                    "error": None,
                }
                with _mot_lock:
                    _mot_progress[sid] = state
                # Write to S3 at most every 2s so cold-start poll instances see live progress
                now = time.time()
                if now - _last_s3_write[0] >= 2.0:
                    _last_s3_write[0] = now
                    threading.Thread(target=_write_progress_to_s3, args=(state,), daemon=True).start()

        if errored_indices:
            logger.warning("[DVSA] Retrying %d failed VRMs after 5s: %s", len(errored_indices), list(errored_indices.values()))
            time.sleep(5)
            for idx, vrm in errored_indices.items():
                try:
                    vehicle_json = client.get_vehicle_by_vrm(vrm)
                    not_found = vehicle_json is DVSA_NOT_FOUND
                    results[idx] = _build_mot_row(vrm, None if not_found else vehicle_json, not_found=not_found)
                    logger.info("[DVSA] Retry succeeded for %s", vrm)
                except Exception as e:
                    logger.error("[DVSA] Retry failed for %s: %s", vrm, e)

        total_elapsed = round(time.time() - start, 1)
        columns, rows, filename, counts = _mot_build_and_upload(results, vrms, user)

        completion = {
            "pct": 100,
            "done": total,
            "total": total,
            "eta_s": 0,
            "complete": True,
            "filename": filename,
            "error": None,
            "counts": counts,
        }
        with _mot_lock:
            _mot_progress[sid] = completion
        _write_progress_to_s3(completion)
        logger.info("[DVSA] MOT check complete for sid=%s: %d VRMs in %.1fs", sid, total, total_elapsed)

    except Exception as e:
        logger.error("[DVSA] MOT background error for sid=%s: %s", sid, e)
        error_state = {
            "pct": 0, "done": 0, "total": total, "eta_s": None,
            "complete": True, "filename": None, "error": str(e),
        }
        with _mot_lock:
            _mot_progress[sid] = error_state
        _write_progress_to_s3(error_state)


def _mot_build_and_upload(results: list, vrms: list, user: str):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from utils.file_handler import DEFAULT_FONT_NAME, set_default_font

    columns = ["VRM", "Make", "Model", "Colour", "Fuel Type",
               "Date Registered", "Most Recent MOT Date", "MOT Expiry Date",
               "Odometer", "Status"]
    max_pairs = max((r["pairs"] for r in results), default=0)
    for i in range(1, max_pairs + 1):
        sfx = ("st" if i == 1 else "nd" if i == 2 else "rd" if i == 3 else "th")
        columns += [f"{i}{sfx} MOT Date", f"{i}{sfx} MOT Result", f"{i}{sfx} MOT Mileage"]
    columns.append("Comments")

    rows = []
    for r in results:
        row = [r["vrm"], r["make"], r["model"], r["colour"], r["fuel"],
               r["date_registered"], r["last_mot_date"], r["expiry_date"],
               r["odometer"], r["status"]]
        for d, m, passed in r["test_pairs"]:
            row += [d, "PASS" if passed else "FAIL", m]
        row += ["", "", ""] * (max_pairs - len(r["test_pairs"]))
        row.append(_build_mot_comments(r))
        rows.append(row)

    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    ts_str = _uk_now().strftime("%Y%m%d_%H%M%S")
    filename = f"mot_check_{ts_str}.xlsx"
    s3_key = _MOT_S3_PREFIX.format(env=env) + filename

    wb = openpyxl.Workbook()
    set_default_font(wb)
    ws = wb.active
    ws.title = "MOT Check"

    header_fill = PatternFill("solid", fgColor="BDD7EE")
    header_font = Font(name=DEFAULT_FONT_NAME, bold=True)
    valid_fill   = PatternFill("solid", fgColor="C6EFCE")
    expired_fill = PatternFill("solid", fgColor="FFC7CE")
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    status_col   = columns.index("Status")   + 1 if "Status"   in columns else None
    comments_col = columns.index("Comments") + 1 if "Comments" in columns else None

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
            if col_idx == status_col:
                val = cell.value or ""
                if val == "VALID":
                    cell.fill = valid_fill
                elif val == "EXPIRED":
                    cell.fill = expired_fill
            elif col_idx == comments_col and cell.value:
                cell.fill = expired_fill

    ws.freeze_panes = "A2"
    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 2, 10), 40)

    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        import boto3 as _boto3
        s3 = _boto3.client('s3', region_name='eu-west-1')
        wb.save(tmp_path)
        expired_count   = sum(1 for r in results if r.get("status") == "EXPIRED")
        unknown_count   = sum(1 for r in results if r.get("status") == "UNKNOWN")
        not_found_count = sum(1 for r in results if r.get("status") == "NOT_FOUND")

        def _has_mileage_warn(r):
            pairs = r.get("test_pairs", [])
            for j in range(1, len(pairs)):
                prev_m = pairs[j-1][1]
                curr_m = pairs[j][1]
                try:
                    if curr_m is not None and prev_m is not None and int(str(curr_m).replace(',','')) < int(str(prev_m).replace(',','')):
                        return True
                except (ValueError, TypeError):
                    pass
            return False

        def _has_early_mot(r):
            import datetime as _dt
            pairs = r.get("test_pairs", [])
            def _parse(s):
                if not s: return None
                for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
                    try: return _dt.datetime.strptime(s, fmt).date()
                    except ValueError: pass
                return None
            dates = [(_parse(p[0]), p[2]) for p in pairs]
            for j in range(1, len(dates)):
                prev, prev_passed = dates[j-1]
                curr, _curr_passed = dates[j]
                if prev and curr:
                    if not prev_passed:
                        continue  # retest after a failure — not an early test
                    try:
                        due = prev.replace(year=prev.year + 1)
                    except ValueError:
                        due = prev.replace(year=prev.year + 1, day=28)
                    if (due - curr).days >= 60:
                        return True
            return False

        mileage_warn_count = sum(1 for r in results if _has_mileage_warn(r))
        early_mot_count    = sum(1 for r in results if _has_early_mot(r))
        valid_count        = sum(1 for r in results if r.get("status") == "VALID")

        metadata = {
            "vrm_count":          str(len(vrms)),
            "valid_count":        str(valid_count),
            "expired_count":      str(expired_count),
            "unknown_count":      str(unknown_count),
            "not_found_count":    str(not_found_count),
            "mileage_warn_count": str(mileage_warn_count),
            "early_mot_count":    str(early_mot_count),
            "processed_by":       user,
        }
        with open(tmp_path, 'rb') as f:
            s3.upload_fileobj(f, s3_bucketname, s3_key, ExtraArgs={"Metadata": metadata})
        # Upload JSON sidecar so load endpoint can serve results without re-parsing XLSX
        json_key = s3_key.replace('.xlsx', '.json')
        s3.put_object(
            Bucket=s3_bucketname, Key=json_key,
            Body=json.dumps({"columns": columns, "rows": rows}).encode(),
            ContentType='application/json',
            Metadata=metadata,
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return columns, rows, filename, {
        "vrm_count":          len(vrms),
        "valid_count":        valid_count,
        "expired_count":      expired_count,
        "unknown_count":      unknown_count,
        "not_found_count":    not_found_count,
        "mileage_warn_count": mileage_warn_count,
        "early_mot_count":    early_mot_count,
    }


@router.get("/mot-check/history")
def mot_check_history(user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    import boto3
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    prefix = _MOT_S3_PREFIX.format(env=env)
    try:
        resp = boto3.client('s3').list_objects_v2(Bucket=s3_bucketname, Prefix=prefix)
        objects = resp.get('Contents', [])
        objects.sort(key=lambda o: o['LastModified'], reverse=True)
        import boto3 as _boto3
        s3 = _boto3.client('s3')

        def _metadata(key):
            try:
                m = s3.head_object(Bucket=s3_bucketname, Key=key).get('Metadata', {})
                def _int(k): return int(m[k]) if m.get(k) else None
                return {
                    "vrm_count":          _int("vrm_count"),
                    "valid_count":        _int("valid_count"),
                    "expired_count":      _int("expired_count"),
                    "unknown_count":      _int("unknown_count"),
                    "not_found_count":    _int("not_found_count"),
                    "mileage_warn_count": _int("mileage_warn_count"),
                    "early_mot_count":    _int("early_mot_count"),
                    "processed_by":       m.get('processed_by'),
                }
            except Exception:
                return {"vrm_count": None, "valid_count": None, "expired_count": None, "unknown_count": None, "not_found_count": None, "mileage_warn_count": None, "early_mot_count": None, "processed_by": None}

        xlsx_objects = [o for o in objects if o['Key'] != prefix and o['Key'].replace(prefix, '').endswith('.xlsx')][:20]

        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _asc
        with _TPE(max_workers=10) as pool:
            futures = {pool.submit(_metadata, o['Key']): o for o in xlsx_objects}
            meta_map = {fut: futures[fut] for fut in futures}
            results_map = {}
            for fut in _asc(futures):
                results_map[id(meta_map[fut])] = fut.result()

        result = []
        for o in xlsx_objects:
            meta = results_map[id(o)]
            result.append({
                "filename":           o['Key'].replace(prefix, ''),
                "ts":                 o['LastModified'].strftime("%d/%m/%Y %H:%M"),
                "vrm_count":          meta["vrm_count"],
                "valid_count":        meta["valid_count"],
                "expired_count":      meta["expired_count"],
                "unknown_count":      meta["unknown_count"],
                "not_found_count":    meta["not_found_count"],
                "mileage_warn_count": meta["mileage_warn_count"],
                "early_mot_count":    meta["early_mot_count"],
                "processed_by":       meta["processed_by"],
            })
        return result
    except Exception as e:
        logger.warning(f"MOT history S3 list failed: {e}")
        return []


@router.get("/mot-check/download/{filename}")
def mot_check_download(filename: str, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    s3_key = _MOT_S3_PREFIX.format(env=env) + filename
    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        download_file(tmp_path, s3_bucketname, s3_key)
        return FileResponse(tmp_path, filename=filename, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=404, detail="File not found in S3")


@router.get("/mot-check/load/{filename}")
def mot_check_load(filename: str, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    import boto3 as _boto3
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    prefix = _MOT_S3_PREFIX.format(env=env)
    s3 = _boto3.client('s3', region_name='eu-west-1')

    # Try JSON sidecar first — much faster than downloading and re-parsing XLSX
    json_key = prefix + filename.replace('.xlsx', '.json')
    try:
        obj = s3.get_object(Bucket=s3_bucketname, Key=json_key)
        return json.loads(obj['Body'].read())
    except Exception:
        pass

    # Fall back to XLSX parsing for older files without a sidecar
    import openpyxl
    s3_key = prefix + filename
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
        rows = []
        for row in rows_iter:
            rows.append([str(c) if c is not None else "" for c in row])
        wb.close()
        return {"columns": columns, "rows": rows}
    except Exception as e:
        raise HTTPException(status_code=404, detail="File not found in S3")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _parse_mot_datetime(s: str):
    if not s:
        return None
    s = s.strip()
    try:
        if "T" in s:
            return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        pass
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _to_ddmmyyyy(s: str) -> str:
    if not s:
        return ""
    dt = _parse_mot_datetime(s)
    if not dt:
        return ""
    return dt.strftime("%d/%m/%Y")




def _build_mot_comments(r: dict) -> str:
    notes = []

    pairs = r.get("test_pairs") or []
    # Mileage discrepancy: each mileage should be >= the previous
    prev_mileage = None
    for _date, mileage_str, _passed in pairs:
        try:
            m = int(str(mileage_str).replace(",", "")) if mileage_str else None
        except ValueError:
            m = None
        if prev_mileage is not None and m is not None and m < prev_mileage:
            notes.append("Mileage discrepancy")
            break
        if m is not None:
            prev_mileage = m

    # Current odometer vs most recent historical mileage
    if pairs:
        try:
            last_m = int(str(pairs[-1][1]).replace(",", "")) if pairs[-1][1] else None
        except ValueError:
            last_m = None
        try:
            odom = int(str(r.get("odometer", "")).replace(",", "")) if r.get("odometer") else None
        except ValueError:
            odom = None
        if last_m is not None and odom is not None and odom < last_m:
            if "Mileage discrepancy" not in notes:
                notes.append("Mileage discrepancy")

    # Early MOT: any MOT done 60+ days before the previous test was due to expire.
    # A test straight after a failure is excluded — the car had to be retested, so it is
    # not early in any suspicious sense. A failed test that is itself early still counts.
    dates = []
    for date_str, _mileage, passed in pairs:
        try:
            d = datetime.datetime.strptime(date_str, "%d/%m/%Y").date() if date_str else None
        except ValueError:
            d = None
        dates.append((d, passed))

    for j in range(1, len(dates)):
        prev_d, prev_passed = dates[j - 1]
        curr_d, _curr_passed = dates[j]
        if prev_d is None or curr_d is None:
            continue
        if not prev_passed:
            continue  # retest after a failure — not an early test
        try:
            due = datetime.date(prev_d.year + 1, prev_d.month, prev_d.day)
        except ValueError:
            due = datetime.date(prev_d.year + 1, 3, 1)
        if (due - curr_d).days >= 60:
            notes.append("Early MOT test")
            break

    return "; ".join(notes)


def _build_mot_row(vrm: str, vehicle_json, not_found: bool = False) -> dict:
    import zoneinfo
    uk_tz = zoneinfo.ZoneInfo("Europe/London")

    if not_found:
        return {"vrm": vrm, "make": "", "model": "", "colour": "", "fuel": "",
                "date_registered": "", "last_mot_date": "", "expiry_date": "",
                "odometer": "", "status": "NOT_FOUND", "test_pairs": [], "pairs": 0}

    empty = {"vrm": vrm, "make": "", "model": "", "colour": "", "fuel": "",
              "date_registered": "", "last_mot_date": "", "expiry_date": "",
              "odometer": "", "status": "UNKNOWN", "test_pairs": [], "pairs": 0}

    if not vehicle_json:
        return empty

    v = vehicle_json[0] if isinstance(vehicle_json, list) and vehicle_json else vehicle_json
    if not isinstance(v, dict):
        return empty

    tests = list(v.get("motTests") or [])
    tests.sort(key=lambda t: (_parse_mot_datetime(t.get("completedDate") or "") or datetime.datetime.min))

    passed_tests = [t for t in tests if (t.get("testResult") or "").upper() == "PASSED"]
    most_recent = passed_tests[-1] if passed_tests else {}
    expiry = _to_ddmmyyyy(most_recent.get("expiryDate") or v.get("motTestExpiryDate") or "")

    status = "UNKNOWN"
    if expiry:
        try:
            exp_d = datetime.datetime.strptime(expiry, "%d/%m/%Y").date()
            today = datetime.datetime.now(uk_tz).date()
            status = "VALID" if exp_d >= today else "EXPIRED"
        except Exception:
            pass

    pairs = [(_to_ddmmyyyy(t.get("completedDate") or ""),
              str(t.get("odometerValue") or "").strip(),
              (t.get("testResult") or "").upper() == "PASSED") for t in tests]

    return {
        "vrm": vrm,
        "make": v.get("make") or "",
        "model": v.get("model") or "",
        "colour": v.get("primaryColour") or "",
        "fuel": v.get("fuelType") or "",
        "date_registered": _to_ddmmyyyy(v.get("firstUsedDate") or ""),
        "last_mot_date": _to_ddmmyyyy(most_recent.get("completedDate") or ""),
        "expiry_date": expiry,
        "odometer": str(most_recent.get("odometerValue") or "").strip(),
        "status": status,
        "test_pairs": pairs,
        "pairs": len(pairs),
    }
