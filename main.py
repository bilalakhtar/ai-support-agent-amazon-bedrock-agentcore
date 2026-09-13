"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # COMPLETED comments.

Reference the step-by-step solution files and INSTRUCTIONS.md for guidance.
Do NOT copy the solution directly — work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.

from pydantic import BaseModel, Field
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CSAI_Agent")

# ── COMPLETED 1 — App Initialisation ───────────────────────────────────────────────
# Create a BedrockAgentCoreApp instance.
# This registers the ASGI server for AgentCore deployment.
# There must be exactly one instance per deployment.
#
# Hint: app = BedrockAgentCoreApp()

# COMPLETED: Create the BedrockAgentCoreApp instance
app = BedrockAgentCoreApp()


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── COMPLETED 2 — Configuration ────────────────────────────────────────────────────
# Replace the placeholder strings with your actual AWS resource values.
# You collected these in Part 1 of the INSTRUCTIONS.
#
# GATEWAY_URL format: https://<alias>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
# KB_ID       format: 10-character alphanumeric string from the KB console
# REGION:     your AWS region, e.g. "us-east-1"
# MEMORY_ID   format: shown in the AgentCore Memory console

GATEWAY_URL = "https://customersupportgateway-gucxaibqjv.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"   # COMPLETED: Replace with your Gateway URL
KB_ID       = "YOZTIAKHHT"                                                                                        # COMPLETED:  Replace with your Knowledge Base ID
REGION      = "us-east-1"                                                                                         # COMPLETED: Replace with your AWS region
MEMORY_ID   = "starter_mem-0y63N152dP"                                                                            # COMPLETED: Replace with your Memory ID


# ── COMPLETED 3 — Model and Clients ────────────────────────────────────────────────
# Create:
#   1. A BedrockModel using model_id "global.amazon.nova-2-lite-v1:0"
#   2. A MemoryClient with region_name=REGION
#   3. A boto3 client for the "bedrock-agent-runtime" service in REGION
#
# Hint: model = BedrockModel(model_id=model_id)

model_id = "global.amazon.nova-2-lite-v1:0"

# COMPLETED: Create the BedrockModel instance
model = BedrockModel(model_id=model_id)

# COMPLETED: Create the MemoryClient instance
memory_client = MemoryClient(region_name=REGION)

# COMPLETED: Create the boto3 bedrock-agent-runtime client
_bedrock_runtime = boto3.client(
    "bedrock-agent-runtime",
    region_name=REGION
)

# ── COMPLETED 4 — Namespace Helper ─────────────────────────────────────────────────
# Implement get_namespaces() to return a dict mapping strategy type to
# namespace template string.
#
# Steps:
#   1. Call mem_client.get_memory_strategies(memory_id) to get strategy list
#   2. Return a dict: { strategy["type"]: strategy["namespaces"][0] for each strategy }
#
# Example output:
#   { "SEMANTIC": "cs_agent/{actorId}/facts",
#     "USER_PREFERENCE": "cs_agent/{actorId}/preferences" }

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)

    namespaces = {}

    for strategy in strategies:
        strategy_type = strategy.get("type")

        # Support both current and legacy AgentCore Memory response fields.
        templates = (
            strategy.get("namespaceTemplates")
            or strategy.get("namespaces")
            or []
        )

        if strategy_type and templates:
            namespaces[strategy_type] = templates[0]

    return namespaces


# ── COMPLETED 5 — Memory Hook ──────────────────────────────────────────────────────
# Implement MemoryHook, a HookProvider subclass that adds long-term memory.
#
# The class needs:
#   __init__(self, actor_id, session_id, memory_client, memory_id)
#     — store all four as instance attributes
#     — call get_namespaces() and store the result as self.namespaces
#
#   retrieve_customer_context(self, event: MessageAddedEvent)
#     — only runs for plain-text user messages (not tool results)
#     — for each strategy namespace, call memory_client.retrieve_memories(
#          memory_id, namespace (formatted with actorId), query, top_k=5)
#     — collect non-empty memory texts tagged with their strategy type
#     — if any memories found, prepend them to the user message as:
#          "Customer Context:\n<memories>\n\n<original_message>"
#
#   save_support_interaction(self, event: AfterInvocationEvent)
#     — walk the message list backwards to find the last plain-text user
#       query and the last assistant response
#     — call memory_client.create_event(memory_id, actor_id, session_id,
#          messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")])
#
#   register_hooks(self, registry: HookRegistry)
#     — register retrieve_customer_context on MessageAddedEvent
#     — register save_support_interaction on AfterInvocationEvent


class LoyaltyDiscountResult(BaseModel):
    points_redeemed: int = Field(ge=0)
    points_discount: float = Field(ge=0)
    tier_discount_pct: int = Field(ge=0, le=100)
    tier_discount: float = Field(ge=0)
    total_savings: float = Field(ge=0)
    final_total: float = Field(ge=0)
    points_earned: int = Field(ge=0)
    remaining_points: int = Field(ge=0)
    display_text: str
    fallback: bool = False


class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        # COMPLETED: Store actor_id, session_id, memory_id, memory_client as attributes
        # COMPLETED: Call get_namespaces() and store the result as self.namespaces

        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id

        self.namespaces = get_namespaces(
            self.memory_client,
            self.memory_id
        )   

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""

        message = event.message

        # Only process user messages.
        if message.get("role") != "user":
            return

        content = message.get("content", [])

        # Ignore tool-result messages; only handle plain-text user input.
        if not content or any("toolResult" in block for block in content):
            return

        # Extract text from the user message.
        query_parts = [
            block["text"]
            for block in content
            if "text" in block and block["text"].strip()
        ]

        if not query_parts:
            return

        query = "\n".join(query_parts)

        memory_lines = []

        for strategy_type, namespace_template in self.namespaces.items():
            namespace = namespace_template.format(actorId=self.actor_id)

            memories = self.memory_client.retrieve_memories(
                memory_id=self.memory_id,
                namespace=namespace,
                query=query,
                top_k=5,
            )

            for memory in memories:
                text = memory.get("content", {}).get("text", "")
                if text:
                    memory_lines.append(
                        f"[{strategy_type}] {text}"
                    )

        if memory_lines:
            context = "\n".join(memory_lines)

            enriched_query = (
                f"Customer Context:\n"
                f"{context}\n\n"
                f"{query}"
            )

            # Replace the text content while keeping this as a normal user message.
            message["content"] = [{"text": enriched_query}]

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        
        messages = event.agent.messages

        customer_query = None
        agent_response = None

        for message in reversed(messages):
            role = message.get("role")
            content = message.get("content", [])

            # Find the latest assistant text response.
            if role == "assistant" and agent_response is None:
                text_parts = [
                    block["text"]
                    for block in content
                    if "text" in block and block["text"].strip()
                ]

                if text_parts:
                    agent_response = "\n".join(text_parts)

            # Find the latest plain-text user message.
            elif role == "user" and customer_query is None:
                # Ignore tool-result user messages.
                if any("toolResult" in block for block in content):
                    continue

                text_parts = [
                    block["text"]
                    for block in content
                    if "text" in block and block["text"].strip()
                ]

                if text_parts:
                    customer_query = "\n".join(text_parts)

                    # If memory context was prepended earlier,
                    # save only the customer's original message.
                    if customer_query.startswith("Customer Context:\n"):
                        parts = customer_query.rsplit("\n\n", 1)
                        if len(parts) == 2:
                            customer_query = parts[1]

            if customer_query and agent_response:
                break

        if not customer_query or not agent_response:
            return

        self.memory_client.create_event(
            memory_id=self.memory_id,
            actor_id=self.actor_id,
            session_id=self.session_id,
            messages=[
                (customer_query, "USER"),
                (agent_response, "ASSISTANT"),
            ],
        )

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(
            MessageAddedEvent,
            self.retrieve_customer_context
        )
        registry.add_callback(
            AfterInvocationEvent,
            self.save_support_interaction
        )


# ── COMPLETED 6 — Knowledge Base Tool ─────────────────────────────────────────────
# Implement search_knowledge_base(query) using the @tool decorator.
#
# Steps:
#   1. Guard: if KB_ID is empty return "Knowledge base not configured."
#   2. Call _bedrock_runtime.retrieve(
#          knowledgeBaseId=KB_ID,
#          retrievalQuery={"text": query}
#      )
#   3. Extract resp["retrievalResults"]; return a message if empty
#   4. Join the text chunks with "\n---\n" and return the result
#
# The docstring is the tool description — the model uses it to decide when
# to call this tool, so keep it clear and accurate.

@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """

    if not KB_ID or not KB_ID.strip():
        return (
            "Knowledge Base is not configured: KB_ID is empty or missing. "
            "Please configure KB_ID before attempting a knowledge-base search."
        )

    if KB_ID.strip().startswith("<"):
        return (
            "Knowledge Base is not configured: KB_ID still contains a placeholder value. "
            "Please configure a valid KB_ID before attempting a knowledge-base search."
        )

    try:
        response = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )

        results = response.get("retrievalResults", [])

        if not results:
            return "No relevant information found in the knowledge base."

        chunks = []

        for result in results:
            text = result.get("content", {}).get("text", "")
            if text:
                chunks.append(text)

        if not chunks:
            return "No relevant information found in the knowledge base."

        return "\n---\n".join(chunks)

    except Exception as e:
        logger.error(f"Knowledge base retrieval failed: {e}")
        return f"Knowledge base search failed: {str(e)}"


# ── COMPLETED 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
# Implement calculate_loyalty_discount() using the @tool decorator.
#
# The tool must:
#   1. Build a self-contained Python code string that:
#        • Defines earn_rates: {"standard": 1, "device": 2, "fresh": 5}
#        • Defines tier_rates: {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
#        • Calculates points_redeemed (floor to nearest 500, cap at 50% of order)
#        • Calculates tier_discount (applied to subtotal after points)
#        • Calculates final_total, total_savings, points_earned, remaining_points
#        • Prints a JSON result dict
#   2. Execute the code with code_session(REGION).invoke("executeCode", {...})
#      using language="python" and clearContext=True
#   3. Return the first result event as a JSON string
#   4. Include a fallback that computes only the tier discount if the
#      Code Interpreter is unavailable

@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """

    code = f"""
import json

earn_rates = {{
    "standard": 1,
    "device": 2,
    "fresh": 5
}}

tier_rates = {{
    "Silver": 0.00,
    "Gold": 0.10,
    "Platinum": 0.15
}}

loyalty_points = {int(loyalty_points)}
tier = {tier!r}
order_total = {float(order_total)}
product_category = {product_category!r}

tier_rate = tier_rates.get(tier, 0.0)
earn_rate = earn_rates.get(product_category, 1)

# 100 points = $1.
# Redemption must be in blocks of 500 points and cannot
# exceed 50% of the original order value.
max_points_for_order = int((order_total * 0.50) * 100)

points_redeemed = min(
    loyalty_points,
    max_points_for_order
)

points_redeemed = (points_redeemed // 500) * 500

points_discount = points_redeemed / 100.0

subtotal_after_points = max(
    0.0,
    order_total - points_discount
)

tier_discount = subtotal_after_points * tier_rate

final_total = round(
    subtotal_after_points - tier_discount,
    2
)

total_savings = round(
    order_total - final_total,
    2
)

points_earned = int(
    final_total * earn_rate
)

remaining_points = (
    loyalty_points
    - points_redeemed
    + points_earned
)

result = {{
    "points_redeemed": points_redeemed,
    "points_discount": round(points_discount, 2),
    "tier_discount_pct": int(tier_rate * 100),
    "tier_discount": round(tier_discount, 2),
    "total_savings": total_savings,
    "final_total": final_total,
    "points_earned": points_earned,
    "remaining_points": remaining_points,
    "display_text": (
        f"Points redeemed: {{points_redeemed}} points. "
        f"Points discount: ${{points_discount:.2f}}. "
        f"Tier discount: {{int(tier_rate * 100)}}% applied to the subtotal "
        f"after points redemption, equal to ${{tier_discount:.2f}}. "
        f"Total savings: ${{total_savings:.2f}}. "
        f"Final total: ${{final_total:.2f}}. "
        f"Points earned: {{points_earned}}. "
        f"Remaining points: {{remaining_points}}."
    )
}}

print(json.dumps(result))
"""

    try:
        with code_session(REGION) as code_client:
            response = code_client.invoke(
                "executeCode",
                {
                    "code": code,
                    "language": "python",
                    "clearContext": True,
                },
            )

            for event in response["stream"]:
                if "result" in event:
                    result = event["result"]

                    stdout = (
                        result.get("structuredContent", {})
                        .get("stdout", "")
                        .strip()
                    )

                    if not stdout:
                        raise ValueError(
                            "Code Interpreter returned no structured output."
                        )

                    validated = LoyaltyDiscountResult.model_validate_json(
                        stdout
                    )

                    return validated.model_dump_json()

        raise RuntimeError(
            "Code Interpreter returned no result."
        )

    except Exception as e:
        logger.warning(
            f"Code Interpreter unavailable; using fallback: {e}"
        )

        tier_rates = {
            "Silver": 0.00,
            "Gold": 0.10,
            "Platinum": 0.15,
        }

        tier_rate = tier_rates.get(tier, 0.0)

        tier_discount = round(
            order_total * tier_rate,
            2
        )

        final_total = round(
            order_total - tier_discount,
            2
        )

        fallback_result = LoyaltyDiscountResult(
            points_redeemed=0,
            points_discount=0.0,
            tier_discount_pct=int(tier_rate * 100),
            tier_discount=tier_discount,
            total_savings=tier_discount,
            final_total=final_total,
            points_earned=0,
            remaining_points=loyalty_points,
            fallback=True,
            display_text=(
                f"Code Interpreter fallback used. "
                f"Points redeemed: 0 points. "
                f"Points discount: $0.00. "
                f"Tier discount: {int(tier_rate * 100)}%, equal to ${tier_discount:.2f}. "
                f"Total savings: ${tier_discount:.2f}. "
                f"Final total: ${final_total:.2f}. "
                f"Points earned: 0. "
                f"Remaining points: {loyalty_points}."
            ),
        )

        return fallback_result.model_dump_json()


# ── COMPLETED 8 — Agent Entrypoint ─────────────────────────────────────────────────
# Implement the invoke() function decorated with @app.entrypoint.
#
# Steps:
#   1. Extract user_input, actor_id, and session_id from the payload
#      (generate a UUID if session_id is missing)
#   2. Instantiate MemoryHook for this actor/session
#   3. Instantiate AgentCoreBrowser(region=REGION)
#   4. Build the tools list: [search_knowledge_base, calculate_loyalty_discount,
#                              agent_core_browser.browser]
#   5. Connect to the Gateway via MCPClient, load gateway_tools, extend tools list
#   6. Create and invoke the Agent with all tools, hooks, and system_prompt
#   7. Return the text from the first content block of the response
#   8. Handle exceptions gracefully

@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """

    try:
        # 1. Extract request values
        user_input = payload.get("prompt", "")
        actor_id = payload.get("customer_id", "anonymous")
        session_id = payload.get("session_id") or str(uuid.uuid4())

        if not user_input:
            return "Please provide a prompt."

        # 2. Long-term memory for this customer/session
        memory_hook = MemoryHook(
            actor_id=actor_id,
            session_id=session_id,
            memory_client=memory_client,
            memory_id=MEMORY_ID,
        )

        # 3. AgentCore Browser
        agent_core_browser = AgentCoreBrowser(region=REGION)

        # 4. Local AgentCore tools
        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]

        system_prompt = """
You are an intelligent customer support assistant for an e-commerce platform.

Use the available tools whenever appropriate:

- Use order-tracker Gateway tools to retrieve orders and customer information.
- Use refund-processor Gateway tools to initiate refunds, check refund status,
  or generate return labels.
- Before initiating a full refund, if the refund amount is not explicitly
  provided by the customer, first call order-tracker___get_order to retrieve
  the order total. Then pass that total as the amount when calling
  refund-processor___initiate_refund.
- Never describe a $0 refund as a full refund.

- Use search_knowledge_base for product information, return policies,
  warranties, loyalty program details, and support documentation.

- Use calculate_loyalty_discount for loyalty points and discount calculations.
- Treat the output of calculate_loyalty_discount as authoritative.
- Never perform your own loyalty arithmetic or recalculate any value.
- Do not introduce or mention any monetary value that is not present in the
  calculate_loyalty_discount tool result.
- When reporting a loyalty calculation, copy the exact values returned by the tool,
  including points_redeemed, points_discount, tier_discount_pct, tier_discount,
  total_savings, final_total, points_earned, and remaining_points.
- The monetary tier discount must exactly match the returned tier_discount field.
- The tier discount is applied after points redemption. Never calculate or state
  what the tier discount would have been on the original order total.
- For loyalty calculations, use the display_text returned by
  calculate_loyalty_discount exactly as the basis of the response.
- Do not independently calculate or introduce additional loyalty values.

- Use the browser tool when the customer asks you to visit or retrieve
  information from a live web page.
- Use Customer Context supplied through memory to personalize responses.
- If any tool fails, returns an authorization error, or cannot retrieve data,
  clearly report the failure and do not guess, infer, or invent the missing information.

Never invent order details, refund results, policies, prices, or calculations.
Use tool results as the source of truth.
Respond clearly and concisely.
"""

        # 5. Connect to AgentCore Gateway through MCP
        gateway_client = MCPClient(
            lambda: streamable_http_client(GATEWAY_URL)
        )
        try:
            with gateway_client:
                try:
                    gateway_tools = gateway_client.list_tools_sync()

                    if not gateway_tools:
                        logger.warning(
                            "Gateway connected but returned no tools."
                        )
                        return (
                            "I'm sorry, the customer-support tools are currently "
                            "unavailable. Please try again later."
                        )

                    tools.extend(gateway_tools)

                    logger.info(
                        "Gateway connected successfully. Loaded %d tools.",
                        len(gateway_tools),
                    )

                except TimeoutError:
                    logger.exception("Gateway tool loading timed out")
                    return (
                        "I'm sorry, the customer-support service timed out. "
                        "Please try again later."
                    )

                except ConnectionError:
                    logger.exception("Gateway connection failed")
                    return (
                        "I'm sorry, I couldn't connect to the customer-support "
                        "service. Please try again later."
                    )

                except Exception as exc:
                    logger.exception(
                        "Gateway tool loading failed: %s",
                        exc,
                    )
                    return (
                        "I'm sorry, the customer-support tools are currently "
                        "unavailable. Please try again later."
                    )

                # 6. Create and invoke the agent while MCP connection is open
                try:
                    agent = Agent(
                        model=model,
                        tools=tools,
                        hooks=[memory_hook],
                        system_prompt=system_prompt,
                    )

                    response = await agent.invoke_async(user_input)

                    return response.message["content"][0]["text"]

                except Exception:
                    logger.exception("Agent invocation failed")
                    return (
                        "Sorry, I encountered an unexpected error while "
                        "processing your request. Please try again later."
                    )

        except TimeoutError:
            logger.exception("Gateway connection timed out")
            return (
                "I'm sorry, the customer-support service timed out. "
                "Please try again later."
            )

        except ConnectionError:
            logger.exception("Gateway connection failed")
            return (
                "I'm sorry, I couldn't connect to the customer-support "
                "service. Please try again later."
            )

        except Exception as exc:
            logger.exception(
                "Gateway connection failed: %s",
                exc,
            )
            return (
                "I'm sorry, the customer-support tools are currently "
                "unavailable. Please try again later."
            )


    except Exception:
        logger.exception("Unexpected request processing failure")
        return (
            "Sorry, I encountered an unexpected error while processing "
            "your request. Please try again later."
        )


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()
