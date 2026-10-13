"""Recognize provider DSML only in model output, never user/website instructions.
Conversions go through the existing registry, argument and call-budget checks.
"""

import json
import re
import uuid
import xml.etree.ElementTree as ET

TAG = re.compile(r"<(/?)[|｜]DSML[|｜](tool_calls|invoke|parameter)\b([^>]*)>")
BLOCK = re.compile(r"<[|｜]DSML[|｜]tool_calls\b[^>]*>.*?</[|｜]DSML[|｜]tool_calls\s*>", re.S)
MARKER = re.compile(r"<[/]?[|｜]DSML[|｜]")


def outside_code(text):
    # Explicit code samples remain examples, rather than becoming tool actions.
    return re.sub(r"```.*?(?:```|$)|`[^`\n]*`", lambda m: " " * len(m.group()), text, flags=re.S)


def has_protocol(text):
    return bool(MARKER.search(outside_code(text)))


def protocol_present(text):
    return bool(MARKER.search(text))


def protocol_requested(question):
    return bool(re.search(r"DSML|tool_calls|工具调用(?:协议|格式)|调用协议", question, re.I))


def decode_calls(text):
    scan = outside_code(text)
    blocks = list(BLOCK.finditer(scan))
    if not blocks:
        if has_protocol(text):
            raise ValueError("Incomplete provider tool protocol")
        return None
    calls = []
    for block in blocks:
        xml = TAG.sub(lambda m: "<" + m[1] + m[2] + m[3] + ">", text[block.start() : block.end()])
        if "<!" in xml or "<?" in xml or len(xml) > 20000:
            raise ValueError("Invalid provider protocol")
        root = ET.fromstring(xml)
        if root.tag != "tool_calls" or root.attrib or (root.text or "").strip():
            raise ValueError("Invalid tool block")
        for invoke in root:
            if (
                invoke.tag != "invoke"
                or set(invoke.attrib) != {"name"}
                or (invoke.text or "").strip()
            ):
                raise ValueError("Invalid invocation")
            arguments = {}
            for parameter in invoke:
                if (
                    parameter.tag != "parameter"
                    or len(parameter)
                    or set(parameter.attrib) != {"name", "string"}
                ):
                    raise ValueError("Invalid parameter")
                key = parameter.attrib["name"]
                raw = parameter.text or ""
                if key in arguments:
                    raise ValueError("Duplicate parameter")
                flag = parameter.attrib["string"]
                if flag not in ("true", "false"):
                    raise ValueError("Invalid parameter type")
                arguments[key] = raw if flag == "true" else json.loads(raw)
                if (parameter.tail or "").strip():
                    raise ValueError("Unexpected invocation text")
            # Compatible providers either wrap the whole arguments object or
            # emit one parameter per JSON property.
            if set(arguments) == {"arguments"}:
                arguments = arguments["arguments"]
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Arguments must be an object")
            calls.append(
                {
                    "id": "dsml-" + uuid.uuid4().hex,
                    "type": "function",
                    "function": {
                        "name": invoke.attrib["name"],
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            )
            if len(calls) > 2:
                raise ValueError("Too many provider invocations")
            if (invoke.tail or "").strip():
                raise ValueError("Unexpected tool block text")
    remaining = scan
    for block in reversed(blocks):
        remaining = remaining[: block.start()] + remaining[block.end() :]
    if MARKER.search(remaining):
        raise ValueError("Incomplete provider tool protocol")
    if not calls:
        raise ValueError("Empty provider tool block")
    return calls
