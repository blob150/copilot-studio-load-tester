"""
Real conversation test against the PFECRM tenant's A2A agent, using the
OFFICIAL Microsoft a2a-sdk (pinned 1.2.3, same as ms_sample/) to build the
protocol-correct request -- instead of hand-guessing JSON-RPC method names.

Auth: confidential client (secret required) via the manual auth-code-flow
helper from probe_a2a_pfecrm.py (reuses its persisted token cache, so no new
sign-in is needed if that cache is still valid).

Run from the pinned venv:
  .venv_a2asdk\\Scripts\\python.exe probe_a2a_pfecrm_sdk.py
"""
import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "ms_sample"))

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import GetExtendedAgentCardRequest, Role, SendMessageRequest, Task, TaskState
from a2a.utils.constants import TransportProtocol

from a2a_output import A2AStreamOutput, completed_text, message_text

from probe_a2a_pfecrm import BASE_URL, auth_headers, get_token  # noqa: E402  (reuse working auth)

BINDINGS = [TransportProtocol.JSONRPC, TransportProtocol.HTTP_JSON]
PENDING = {TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING}


async def main() -> int:
    token = get_token()
    print("Token acquired OK.\n")
    async def log_request(request: httpx.Request) -> None:
        body = request.content.decode("utf-8", errors="replace") if request.content else ""
        print(f"\n--> {request.method} {request.url}")
        print(f"    Accept: {request.headers.get('accept')}")
        if body:
            print(f"    Body: {body[:500]}")

    async def log_response(response: httpx.Response) -> None:
        print(f"<-- HTTP {response.status_code} ({response.headers.get('content-type')})")

    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx.AsyncClient(
            headers=auth_headers(token),
            follow_redirects=False,
            timeout=httpx.Timeout(30, read=120),
            event_hooks={"request": [log_request], "response": [log_response]},
        ))
        print(f"Resolving agent card at {BASE_URL} ...")
        card = await A2ACardResolver(http, base_url=BASE_URL).get_agent_card(
            relative_card_path=".well-known/agent-card.json"
        )
        print(f"Agent: {card.name}")
        for skill in card.skills:
            print(f"  skill: {skill.name} [{skill.id}]: {skill.description}")

        config = ClientConfig(
            streaming=True, polling=False, httpx_client=http,
            supported_protocol_bindings=BINDINGS, accepted_output_modes=["text/plain"],
        )
        client = await create_client(card, config)
        stack.push_async_callback(client.close)

        if card.capabilities.extended_agent_card:
            card = await client.get_extended_agent_card(GetExtendedAgentCardRequest())
            client = await create_client(card, config)
            stack.push_async_callback(client.close)

        prompt = "Hello, can you tell me what you can help with?"
        print(f"\nSending: {prompt!r}\n")
        request = SendMessageRequest(message=new_text_message(prompt, role=Role.ROLE_USER))
        task = Task()
        output = A2AStreamOutput()
        final_text = None

        async for event in client.send_message(request):
            if event.HasField("message"):
                final_text = message_text(event.message)
                output.message(final_text)
            elif event.HasField("task"):
                snapshot = event.task
                task.id = snapshot.id
                task.status.CopyFrom(snapshot.status)
                del task.history[:]
                task.history.extend(snapshot.history)
                for artifact in snapshot.artifacts:
                    output.update(artifact, False)
            elif event.HasField("artifact_update"):
                output.update(event.artifact_update.artifact, event.artifact_update.append)
            elif event.HasField("status_update"):
                task.status.CopyFrom(event.status_update.status)
            else:
                print("Unknown event type received:", event)

        output.finish_line()
        if final_text is None and task.id and task.status.state == TaskState.TASK_STATE_COMPLETED:
            final_text = output.text() or completed_text(task)

        print("\n=== FINAL RESPONSE ===")
        print(final_text or "(no text captured)")
        print(f"\nTask state: {TaskState.Name(task.status.state) if task.id else 'n/a (immediate message)'}")
        print(f"Context ID: {task.context_id or '(from message)'}")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
