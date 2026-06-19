# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import json
import logging
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from google.adk.cli.fast_api import get_fast_api_app

from expense_agent.app_utils.telemetry import setup_telemetry
from expense_agent.app_utils.typing import Feedback

# Set up standard python logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("expense_agent")

setup_telemetry()

# Normalize Pub/Sub subscription names to short names for readable userIds
class NormalizeSubscriptionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.endswith("/trigger/pubsub") and request.method == "POST":
            try:
                # Read request body
                body = await request.body()
                if body:
                    data = json.loads(body)
                    if "subscription" in data and data["subscription"]:
                        sub = data["subscription"]
                        # Extract the subscription name after the last slash
                        if "/" in sub:
                            short_sub = sub.split("/")[-1]
                            data["subscription"] = short_sub
                            new_body = json.dumps(data).encode("utf-8")
                            
                            # Override the receive channel to return the modified body
                            async def receive():
                                return {"type": "http.request", "body": new_body, "more_body": False}
                            request._receive = receive
                            logger.info(f"Normalized subscription path '{sub}' to '{short_sub}'")
            except Exception as e:
                logger.error(f"Error in subscription normalization middleware: {e}")
        return await call_next(request)

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")
AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
session_service_uri = None
artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=False,
    trigger_sources=["pubsub"],
)

app.title = "ambient-expense-agent"
app.description = "API for interacting with the Agent ambient-expense-agent"

# Add the subscription normalization middleware
app.add_middleware(NormalizeSubscriptionMiddleware)

@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback."""
    logger.info(f"Feedback received: {feedback.model_dump()}")
    return {"status": "success"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
