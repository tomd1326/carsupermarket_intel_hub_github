"""Settings > GA4 Requests: the GA4 Data API requests run by the
common_google_analytics_ingestion job, and the Snowflake tables each one ends
up in. A list kept by hand on the page: the Rundeck job steps are not read from
Rundeck, so a step changed there must be changed here too.

Each request is stored in kpi_layout under 'ga4_requests':
    {query_name, description, dimensions, metrics, filter, start_date,
     unique_key, used_by, notes}

Everything else shown on the page (Rundeck command, S3 folder, dbt models,
Snowflake table and columns) is worked out from those, by the same naming the
ingestion job and the uk_dbt_raw_data ux_tracking models use:
    S3    ga4/ga4_<query>/json/v1/last_upload/
    dbt   stg_ga4_<query>__s -> stg_ga4_<query>_dedup__s -> ga4_<query>__s
    table SOURCE_UX_TRACKING.GA4_<QUERY>
"""

import json
import re

QUERY_NAME = r"[a-z][a-z0-9_]{0,60}"
GA4_FIELD = r"[A-Za-z][A-Za-z0-9_]*(:[A-Za-z0-9_]+)?"
REQUESTS_MAX = 100
TEXT_MAX = 2000
_FIELDS = {"query_name", "description", "dimensions", "metrics", "filter", "start_date",
           "unique_key", "used_by", "notes"}
SCHEMA = "SOURCE_UX_TRACKING"

# The Rundeck job steps as they were on 2026-10-23, used until the list is
# first saved. used_by and unique_key are from the uk_dbt_* models that day.
SEED = [
    {"query_name": "total_sessions", "dimensions": "date,sessionSourceMedium,sessionCustomChannelGroup:14277941516",
     "metrics": "sessions", "start_date": True,
     "unique_key": "date, session_custom_channel_group, session_source_medium",
     "used_by": "ga4_daily_metrics__c"},
    {"query_name": "total_sessions_daily", "dimensions": "date", "metrics": "sessions", "start_date": True,
     "unique_key": "date", "used_by": "daily_summary__p"},
    {"query_name": "total_sessions_by_page", "dimensions": "date,pagePath", "metrics": "sessions",
     "unique_key": "date, page_path", "used_by": "ga4_daily_metrics__c"},
    {"query_name": "total_pageview", "dimensions": "date,pagePath,eventName", "metrics": "eventCount",
     "unique_key": "date, event_name, page_path", "used_by": "ga4_daily_events__c, ga4_daily_metrics__c"},
    {"query_name": "vdp_pageview_volume", "dimensions": "date,customEvent:page_type,eventName", "metrics": "eventCount",
     "unique_key": "date, event_name, custom_event_page_type", "used_by": ""},
    {"query_name": "total_lead_volume", "dimensions": "date,eventName", "metrics": "eventCount",
     "unique_key": "date, event_name", "used_by": "ga4_daily_metrics__c"},
    {"query_name": "vdp_bounce_rate", "dimensions": "date,pagePath,customEvent:page_type,sessionSourceMedium",
     "metrics": "bounceRate",
     "unique_key": "date, page_path, custom_event_page_type, session_source_medium", "used_by": ""},
    {"query_name": "total_bounce_rate", "dimensions": "date,pagePath,sessionSourceMedium", "metrics": "bounceRate,sessions",
     "unique_key": "date, page_path, session_source_medium", "used_by": "ga4_daily_metrics__c"},
    {"query_name": "total_users", "dimensions": "date,pagePath,sessionSourceMedium", "metrics": "totalUsers",
     "unique_key": "date, page_path, session_source_medium", "used_by": ""},
    {"query_name": "total_users_daily", "dimensions": "date", "metrics": "totalUsers", "start_date": True,
     "unique_key": "date", "used_by": "daily_summary__p",
     "notes": "The Rundeck step's --start-date part looked broken when copied on 2026-10-23 "
              "(\"-start-date $(date d ...\" in front of COMMAND). Check the step in Rundeck."},
    {"query_name": "lead_volume_by_campaign", "dimensions": "date,eventName,sessionCampaignId,sessionCampaignName",
     "metrics": "eventCount", "unique_key": "date, event_name, session_campaign_id, session_campaign_name",
     "used_by": "ga4_campaign_metrics__c"},
    {"query_name": "sessions_by_campaign", "dimensions": "date,sessionCampaignId,sessionCampaignName",
     "metrics": "sessions", "unique_key": "date, session_campaign_id, session_campaign_name",
     "used_by": "ga4_campaign_metrics__c"},
]


def with_defaults(req: dict) -> dict:
    out = {"query_name": "", "description": "", "dimensions": "", "metrics": "", "filter": "",
           "start_date": False, "unique_key": "", "used_by": "", "notes": ""}
    out.update({k: v for k, v in req.items() if k in _FIELDS})
    return out


def split_fields(text: str) -> list:
    return [f.strip() for f in (text or "").split(",") if f.strip()]


def column_name(field: str) -> str:
    """The Snowflake column for a GA4 field, as the stg models name it:
    sessionSourceMedium -> session_source_medium,
    customEvent:page_type -> custom_event_page_type,
    sessionCustomChannelGroup:14277941516 -> session_custom_channel_group (the id is dropped)."""
    name, _, suffix = field.partition(":")
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()
    if suffix and not suffix.isdigit():
        snake += "_" + suffix.lower()
    return snake


def derived(req: dict) -> dict:
    """Everything the page shows that follows from the stored fields."""
    q = req["query_name"]
    fields = split_fields(req["dimensions"]) + split_fields(req["metrics"])
    return {
        "command": command(req),
        "s3_folder": f"ga4/ga4_{q}/json/v1/last_upload/",
        "dbt_models": [f"stg_ga4_{q}__s", f"stg_ga4_{q}_dedup__s", f"ga4_{q}__s"],
        "table": f"{SCHEMA}.GA4_{q.upper()}",
        "columns": [{"field": f, "column": column_name(f)} for f in fields],
    }


def command(req: dict) -> str:
    """The Rundeck step's COMMAND line."""
    parts = ["python main.py --company $(echo ${job.project} | tr 'A-Z' 'a-z')",
             f"--query-name {req['query_name']}",
             f"--dimensions '{req['dimensions']}'",
             f"--metrics '{req['metrics']}'" if "," in req["metrics"] else f"--metrics {req['metrics']}"]
    if req.get("filter"):
        parts.append(f"--filter '{req['filter']}'")
    parts.append("--env ${option.ENVIRONMENT}")
    if req.get("start_date"):
        parts.append("--start-date $(date -d ${option.START_DATE}' days ago' -I)")
    return " ".join(parts)


def validate(req, others: list) -> str | None:
    """Error for one request, or None. others: the other saved requests."""
    if not isinstance(req, dict) or set(req) - _FIELDS:
        return "Bad request"
    q = req.get("query_name")
    if not isinstance(q, str) or not re.fullmatch(QUERY_NAME, q):
        return "Query names are lower case letters, numbers and _ (e.g. total_sessions)"
    if any(o.get("query_name") == q for o in others):
        return f"There is already a request called {q}"
    for key, label in (("dimensions", "dimension"), ("metrics", "metric")):
        fields = split_fields(req.get(key))
        if not fields:
            return f"Enter at least one {label}"
        bad = [f for f in fields if not re.fullmatch(GA4_FIELD, f)]
        if bad:
            return f"Bad {label} name: {bad[0]}"
    if req.get("filter"):
        try:
            json.loads(req["filter"])
        except (TypeError, ValueError):
            return "The filter must be JSON, e.g. {\"field\":\"eventName\",\"op\":\"EXACT\",\"value\":\"page_view\"}"
        if "'" in req["filter"]:
            return "The filter cannot contain ' (it is wrapped in ' in the command)"
    if not isinstance(req.get("start_date", False), bool):
        return "Bad start date setting"
    for key in ("description", "unique_key", "used_by", "notes", "filter"):
        v = req.get(key, "")
        if not isinstance(v, str) or len(v) > TEXT_MAX:
            return f"{key.replace('_', ' ').title()} must be text of at most {TEXT_MAX} characters"
    return None


def clean(req: dict) -> dict:
    out = with_defaults(req)
    for key in ("query_name", "description", "filter", "unique_key", "used_by", "notes"):
        out[key] = out[key].strip()
    out["dimensions"] = ",".join(split_fields(out["dimensions"]))
    out["metrics"] = ",".join(split_fields(out["metrics"]))
    return out
