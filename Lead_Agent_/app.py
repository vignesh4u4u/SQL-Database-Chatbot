import asyncio

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import config
from functions_ import fetch_lead_generation, start_lead_generation

app = FastAPI(title="B2B ICP Lead Discovery & Qualification System")


@app.middleware("http")
async def refresh_settings_per_request(request: Request, call_next):
    try:
        await asyncio.to_thread(config.load_settings)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": f"Settings configuration error: {exc}"})
    return await call_next(request)


@app.post("/lead-generation")
async def create_lead_generation(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON payload"})

    status_code, payload = start_lead_generation(data)
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/lead-generation/{job_id}")
def get_lead_generation(job_id: str):
    status_code, payload = fetch_lead_generation(job_id)
    return JSONResponse(status_code=status_code, content=payload)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
