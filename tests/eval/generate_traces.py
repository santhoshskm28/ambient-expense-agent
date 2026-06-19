import os
import json
import asyncio
from google.adk.runners import InMemoryRunner
from google.genai import types

from expense_agent.agent import app

# Create trace output directories
os.makedirs("artifacts/traces", exist_ok=True)

def serialize_content(content):
    if not content:
        return None
    
    parts_json = []
    for part in content.parts or []:
        part_json = {}
        if part.text is not None:
            part_json["text"] = part.text
        if part.function_call is not None:
            fc = part.function_call
            part_json["function_call"] = {
                "name": fc.name,
                "args": fc.args,
                "id": fc.id
            }
        if part.function_response is not None:
            fr = part.function_response
            part_json["function_response"] = {
                "name": fr.name,
                "id": fr.id,
                "response": fr.response
            }
        if part_json:
            parts_json.append(part_json)
            
    return {
        "role": content.role or "model",
        "parts": parts_json
    }

async def run_scenario(case_id, raw_prompt_text):
    print(f"\n--- Running Scenario: {case_id} ---")
    
    runner = InMemoryRunner(app=app)
    user_id = "eval_user"
    session = await runner.session_service.create_session(
        app_name="expense_agent", user_id=user_id
    )
    
    prompt_content = types.Content(
        role="user",
        parts=[types.Part.from_text(text=raw_prompt_text)]
    )
    
    turns = []
    current_turn_events = []
    
    # 1. Turn 0 User Prompt Event
    current_turn_events.append({
        "author": "user",
        "content": serialize_content(prompt_content)
    })
    
    # 2. Run initial workflow until pause/complete
    interrupt_id = None
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=prompt_content
    ):
        # We only care about events that produce UI content (text or function call)
        if event.content and event.content.parts:
            # Check if this event contains a request_input function call
            for part in event.content.parts:
                if part.function_call and part.function_call.name == "adk_request_input":
                    interrupt_id = part.function_call.id
            
            author = event.author or "expense_agent"
            current_turn_events.append({
                "author": author,
                "content": serialize_content(event.content)
            })
            
    turns.append({
        "turn_index": 0,
        "events": current_turn_events
    })
    
    # 3. If paused, automate HITL decision and resume
    if interrupt_id:
        print(f"Workflow paused on interrupt: {interrupt_id}. Automating decision...")
        
        # Decide based on case details
        parsed = json.loads(raw_prompt_text)
        desc = parsed.get("description", "").lower()
        amount = parsed.get("amount", 0)
        
        # Injection detection in description
        injection_keywords = ["bypass", "override", "auto-approve", "auto approve"]
        is_injection = any(k in desc for k in injection_keywords)
        
        if is_injection:
            decision = {"approved": False, "notes": "Rejected: Security event/injection attempt detected."}
        else:
            decision = {"approved": True, "notes": "Approved by automated trace manager."}
            
        print(f"Decision: {decision}")
        
        # Create function response to resume
        part = types.Part(
            function_response=types.FunctionResponse(
                name="adk_request_input",
                id=interrupt_id,
                response=decision
            )
        )
        resume_message = types.Content(role="user", parts=[part])
        
        # Turn 1 events starting with the function response from "user"
        turn_1_events = [{
            "author": "user",
            "content": serialize_content(resume_message)
        }]
        
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=resume_message
        ):
            if event.content and event.content.parts:
                author = event.author or "expense_agent"
                turn_1_events.append({
                    "author": author,
                    "content": serialize_content(event.content)
                })
                
        turns.append({
            "turn_index": 1,
            "events": turn_1_events
        })
        
    print(f"Scenario {case_id} completed. Total turns: {len(turns)}")
    return turns

async def main():
    with open("tests/eval/datasets/basic-dataset.json", "r") as f:
        dataset = json.load(f)
        
    output_cases = []
    
    for case in dataset["eval_cases"]:
        case_id = case["eval_case_id"]
        raw_prompt_text = case["prompt"]["parts"][0]["text"]
        
        turns = await run_scenario(case_id, raw_prompt_text)
        
        output_cases.append({
            "eval_case_id": case_id,
            "agent_data": {
                "agents": {
                    "expense_agent": {
                        "agent_id": "expense_agent",
                        "instruction": "Corporate Expense Management Workflow"
                    }
                },
                "turns": turns
            }
        })
        
    output_dataset = {"eval_cases": output_cases}
    
    with open("artifacts/traces/generated_traces.json", "w") as f:
        json.dump(output_dataset, f, indent=2)
    print("\nTraces successfully generated and written to artifacts/traces/generated_traces.json")

if __name__ == "__main__":
    asyncio.run(main())
