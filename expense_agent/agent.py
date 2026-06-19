import os
import re
import json
import math
import base64
from enum import Enum
from datetime import datetime
from typing import Any, AsyncGenerator, Union, Optional
from pydantic import BaseModel, Field, field_validator, ValidationError

from dotenv import load_dotenv

load_dotenv()

# Set up local authentication
if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
    import google.auth

    try:
        _, project_id = google.auth.default()
        os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
        if not os.environ.get("GOOGLE_CLOUD_LOCATION"):
            os.environ["GOOGLE_CLOUD_LOCATION"] = "us-east1"
    except Exception:
        pass

from google.adk.agents import LlmAgent
from google.adk.models import Gemini
from google.adk.apps import App
from google.adk.workflow import Workflow, START
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.genai import types

from .config import AUTO_APPROVE_THRESHOLD, MODEL_NAME

# =====================================================================
# 1. Data Schemas
# =====================================================================


class ExpenseCategory(str, Enum):
    TRAVEL = "travel"
    MEALS = "meals"
    SOFTWARE = "software"
    HARDWARE = "hardware"
    OFFICE = "office"
    MEDICAL = "medical"
    ENTERTAINMENT = "entertainment"
    GENERAL = "general"
    OTHER = "other"


class ExpenseReport(BaseModel):
    amount: float
    submitter: str
    category: str
    description: str
    date: str
    pii_redacted: Optional[bool] = False
    redacted_fields: Optional[list[str]] = None
    security_alert: Optional[bool] = False
    security_reason: Optional[str] = None

    @field_validator("amount")
    @classmethod
    def amount_must_be_valid(cls, v: float) -> float:
        import math

        if math.isnan(v) or math.isinf(v):
            raise ValueError("Amount must be a finite number")
        if v < 0:
            raise ValueError("Amount must not be negative")
        if v > 1_000_000:
            raise ValueError("Amount exceeds maximum allowed ($1,000,000)")
        return round(v, 2)

    @field_validator("submitter")
    @classmethod
    def submitter_must_be_email(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", v):
            raise ValueError("Submitter must be a valid email address")
        return v.lower()

    @field_validator("category")
    @classmethod
    def category_must_be_known(cls, v: str) -> str:
        v_lower = v.lower().strip()
        valid_categories = {e.value for e in ExpenseCategory}
        if v_lower not in valid_categories:
            return ExpenseCategory.OTHER.value
        return v_lower

    @field_validator("date")
    @classmethod
    def date_must_be_valid(cls, v: str) -> str:
        if not v:
            return ""
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Date must be in YYYY-MM-DD format")
        return v


class RiskAssessment(BaseModel):
    risk_score: int = Field(description="Risk score from 1 (low) to 5 (high)")
    findings: str = Field(
        description="Detailed findings about compliance or risk factors"
    )
    alert_raised: bool = Field(description="Whether a high risk alert is raised")


class ApprovalDecision(BaseModel):
    approved: bool
    notes: str


# =====================================================================
# 2. Workflow Nodes
# =====================================================================


def parse_event(ctx: Context, node_input: Any) -> Event:
    """Parses incoming event payload from Pub/Sub or local test JSON."""
    raw_data = None
    if isinstance(node_input, dict):
        raw_data = node_input
    elif hasattr(node_input, "parts") and node_input.parts:
        text = node_input.parts[0].text
        try:
            raw_data = json.loads(text)
        except Exception:
            raw_data = {"description": text}
    elif isinstance(node_input, str):
        try:
            raw_data = json.loads(node_input)
        except Exception:
            raw_data = {"description": node_input}
    else:
        raw_data = node_input or {}

    # Extract Pub/Sub message envelope if present
    message_dict = raw_data
    if "message" in raw_data and isinstance(raw_data["message"], dict):
        message_dict = raw_data["message"]

    expense_data = {}
    if "data" in message_dict:
        data_payload = message_dict["data"]
        if isinstance(data_payload, str):
            try:
                decoded = base64.b64decode(data_payload).decode("utf-8")
                expense_data = json.loads(decoded)
            except Exception:
                try:
                    expense_data = json.loads(data_payload)
                except Exception:
                    expense_data = {"description": data_payload}
        elif isinstance(data_payload, dict):
            expense_data = data_payload
    else:
        expense_data = raw_data

    amount = float(expense_data.get("amount", 0.0))
    submitter = expense_data.get("submitter", "unknown@company.com")
    category = expense_data.get("category", "general")
    description = expense_data.get("description", "")
    date = expense_data.get("date", "")

    try:
        expense = ExpenseReport(
            amount=amount,
            submitter=submitter,
            category=category,
            description=description,
            date=date,
        )
    except (ValidationError, ValueError) as e:
        rejection = {
            "status": "REJECTED",
            "reason": f"Invalid expense data: {e}",
            "amount": amount,
            "submitter": submitter,
            "date": date,
            "pii_redacted": False,
            "security_alert": True,
            "notes": "Rejected due to invalid input data",
        }
        return Event(
            output=rejection, route="rejected", state={"final_decision": rejection}
        )

    if expense.amount < AUTO_APPROVE_THRESHOLD:
        return Event(output=expense, route="auto_approve")
    else:
        return Event(output=expense, route="security_review")


def security_screen(node_input: ExpenseReport) -> Event:
    """Security screen to redact PII and detect prompt injection."""
    expense = node_input.model_copy()
    desc = expense.description
    redacted_fields = []

    # Redact SSNs and Credit Cards
    ssn_pattern = r"\b\d{3}-\d{2}-\d{4}\b|\b\d{9}\b"
    cc_pattern = r"\b(?:\d[ -]*?){13,16}\b"

    if re.search(ssn_pattern, desc):
        desc = re.sub(ssn_pattern, "[REDACTED SSN]", desc)
        redacted_fields.append("ssn")
    if re.search(cc_pattern, desc):
        desc = re.sub(cc_pattern, "[REDACTED CREDIT CARD]", desc)
        redacted_fields.append("credit_card")

    expense.description = desc
    if redacted_fields:
        expense.pii_redacted = True
        expense.redacted_fields = redacted_fields

    # Check for prompt injection
    injection_keywords = [
        "bypass",
        "override",
        "auto-approve",
        "auto approve",
        "ignore rules",
        "ignore the rules",
        "force approval",
        "grant access",
        "auto_approve",
    ]
    has_injection = any(keyword in desc.lower() for keyword in injection_keywords)

    if has_injection:
        expense.security_alert = True
        expense.security_reason = "Detected potential prompt injection in description."
        return Event(
            output=expense, route="bypass_llm", state={"expense": expense.model_dump()}
        )

    return Event(output=expense, route="clean", state={"expense": expense.model_dump()})


def auto_approve(node_input: ExpenseReport) -> Event:
    """Instantly auto-approves low-value expenses without LLM cost."""
    decision = {
        "status": "APPROVED",
        "reason": "Auto-approved (expense under $100)",
        "amount": node_input.amount,
        "submitter": node_input.submitter,
        "date": node_input.date,
        "pii_redacted": False,
        "security_alert": False,
        "notes": "Auto-approved",
    }
    return Event(output=decision, state={"final_decision": decision})


# LLM node: assesses risk
review_agent = LlmAgent(
    name="review_agent",
    model=Gemini(
        model=MODEL_NAME,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are a compliance risk reviewer.
Analyze the expense report for any compliance issues or risk factors.
Provide a risk score from 1 (low) to 5 (high) and detailed findings.
If the expense is suspicious, raise an alert.""",
    output_schema=RiskAssessment,
    output_key="risk_assessment",
)


async def human_approval(
    ctx: Context, node_input: Any
) -> AsyncGenerator[Union[RequestInput, Event], None]:
    """Pauses the workflow for human approval and reads responses."""
    expense_dict = ctx.state.get("expense", {})
    if not expense_dict and isinstance(node_input, ExpenseReport):
        expense_dict = node_input.model_copy().model_dump()
    elif not expense_dict and isinstance(node_input, dict) and "amount" in node_input:
        expense_dict = node_input

    risk_dict = ctx.state.get("risk_assessment", {})

    if not ctx.resume_inputs:
        message = f"Expense report from {expense_dict.get('submitter')} for ${expense_dict.get('amount'):.2f} requires approval.\n"
        message += f"Description: {expense_dict.get('description')}\n"
        if expense_dict.get("security_alert"):
            message += f"⚠️ SECURITY WARNING: {expense_dict.get('security_reason')}\n"
        if risk_dict:
            message += f"Risk Score: {risk_dict.get('risk_score')}/5\n"
            message += f"Risk Findings: {risk_dict.get('findings')}\n"

        yield RequestInput(
            interrupt_id="approval_decision", message=message, schema=ApprovalDecision
        )
        return

    response = ctx.resume_inputs.get("approval_decision")
    if isinstance(response, dict):
        decision = ApprovalDecision(**response)
    else:
        decision = response

    status = "APPROVED" if decision.approved else "REJECTED"
    final_decision = {
        "status": status,
        "notes": decision.notes,
        "amount": expense_dict.get("amount"),
        "submitter": expense_dict.get("submitter"),
        "date": expense_dict.get("date"),
        "pii_redacted": expense_dict.get("pii_redacted", False),
        "security_alert": expense_dict.get("security_alert", False),
        "risk_score": risk_dict.get("risk_score") if risk_dict else None,
    }

    yield Event(output=final_decision, state={"final_decision": final_decision})


def record_outcome(ctx: Context, node_input: Any) -> Event:
    """Records the outcome, prints logs, and returns a UI-friendly event."""
    decision = ctx.state.get("final_decision", {})
    if not decision:
        decision = node_input

    status = decision.get("status")
    amount = decision.get("amount", 0.0)
    notes = decision.get("notes", "Auto-approved")
    text = f"Expense of ${amount:.2f} was {status}. Notes: {notes}"

    return Event(
        output=decision,
        content=types.Content(role="model", parts=[types.Part.from_text(text=text)]),
    )


# =====================================================================
# 3. Graph Wiring
# =====================================================================

edges = [
    (START, parse_event),
    (parse_event, {"auto_approve": auto_approve, "security_review": security_screen}),
    (security_screen, {"clean": review_agent, "bypass_llm": human_approval}),
    (review_agent, human_approval),
    (human_approval, record_outcome),
    (auto_approve, record_outcome),
]

root_agent = Workflow(
    name="expense_agent",
    description="Corporate Expense Management Workflow",
    edges=edges,
)

app = App(
    root_agent=root_agent,
    name="expense_agent",
)
