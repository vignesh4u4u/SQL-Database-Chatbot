import asyncio
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient
from pymongo.collection import Collection

import config
from lead_search_agent import discover_leads, logger

_MONGO_CLIENT: MongoClient | None = None
_MONGO_URI_CACHED: str | None = None


def _get_jobs_collection() -> Collection:
    global _MONGO_CLIENT, _MONGO_URI_CACHED
    config.load_settings()
    mongo_uri = os.getenv("MONGO_URI", "").strip()
    if not mongo_uri:
        raise ValueError("MONGO_URI is missing from environment.")
    if _MONGO_CLIENT is None or _MONGO_URI_CACHED != mongo_uri:
        if _MONGO_CLIENT is not None:
            try:
                _MONGO_CLIENT.close()
            except Exception:
                pass
        _MONGO_CLIENT = MongoClient(mongo_uri, serverSelectionTimeoutMS=8000)
        _MONGO_URI_CACHED = mongo_uri
    db_name = os.getenv("MONGO_DB_NAME", "").strip()
    db = _MONGO_CLIENT[db_name] if db_name else _MONGO_CLIENT.get_default_database(default="b2b_leads")
    collection = db[os.getenv("MONGO_JOBS_COLLECTION", "lead_generation_jobs")]
    return collection


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execute_lead_job(job_id: str, icp_prompt: str, exclude_companies: list[str]) -> None:
    collection = _get_jobs_collection()
    try:
        response = asyncio.run(discover_leads(icp_prompt, exclude_companies=exclude_companies))
        collection.update_one(
            {"job_id": job_id},
            {
                "$set": {
                    "status": "sucess",
                    "updated_at": _utc_now(),
                    "result": response.model_dump(),
                    "error": None,
                }
            },
        )
        logger.info("Job %s completed with status=sucess | matched_count=%s", job_id, response.matched_count)
    except Exception as exc:
        logger.exception("Job %s failed: %s", job_id, exc)
        collection.update_one(
            {"job_id": job_id},
            {
                "$set": {
                    "status": "failed",
                    "updated_at": _utc_now(),
                    "error": str(exc),
                }
            },
        )


def start_lead_generation(data: Any) -> tuple[int, dict[str, Any]]:
    if not isinstance(data, dict):
        return 400, {"error": "Payload must be a JSON object"}
    icp_prompt = data.get("icp_prompt")
    if not isinstance(icp_prompt, str) or len(icp_prompt.strip()) < 10:
        return 400, {"error": "icp_prompt must be a string with at least 10 characters"}
    raw_exclude = data.get("exclude_companies") or []
    if not isinstance(raw_exclude, list) or not all(isinstance(x, str) for x in raw_exclude):
        return 400, {"error": "exclude_companies must be a list of strings"}

    try:
        collection = _get_jobs_collection()
        job_id = str(uuid.uuid4())
        now = _utc_now()
        collection.insert_one(
            {
                "job_id": job_id,
                "status": "process",
                "icp_prompt": icp_prompt,
                "exclude_companies": raw_exclude,
                "created_at": now,
                "updated_at": now,
                "result": None,
                "error": None,
            }
        )
    except Exception as exc:
        logger.exception("Failed to initialize MongoDB job: %s", exc)
        return 500, {"error": f"Database error: {exc}"}

    worker = threading.Thread(
        target=_execute_lead_job,
        args=(job_id, icp_prompt, raw_exclude),
        daemon=True,
    )
    worker.start()
    logger.info("Job %s created with status=process", job_id)
    return 202, {"job_id": job_id, "status": "process"}


def fetch_lead_generation(job_id: str) -> tuple[int, dict[str, Any]]:
    try:
        collection = _get_jobs_collection()
        doc = collection.find_one({"job_id": job_id}, {"_id": 0})
    except Exception as exc:
        logger.exception("Failed to fetch MongoDB job %s: %s", job_id, exc)
        return 500, {"error": f"Database error: {exc}"}

    if not doc:
        return 404, {"job_id": job_id, "error": "Job not found"}

    status = doc.get("status", "process")
    if status == "sucess":
        result = doc.get("result") or {}
        return 200, {"job_id": job_id, "status": "sucess", **result}
    if status == "failed":
        return 200, {"job_id": job_id, "status": "failed", "error": doc.get("error")}
    return 200, {"job_id": job_id, "status": "process"}
