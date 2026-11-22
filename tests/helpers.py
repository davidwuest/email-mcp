import asyncio
import json

import simple_email_mcp


def call(action, params=None):
    return asyncio.run(simple_email_mcp.email_dispatcher(action, params))


def call_json(action, params=None):
    return json.loads(call(action, params))
