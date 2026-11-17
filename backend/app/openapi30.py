"""Convert the OpenAPI 3.1 document FastAPI generates into OpenAPI 3.0.3 (DC-05).

The differences that occur in this API are: nullable types expressed as
anyOf-with-null, `const`, `examples` arrays, numeric exclusive bounds and
binary upload fields described with contentMediaType.
"""
from copy import deepcopy

_NULL = {"type": "null"}


def _convert(node):
    if isinstance(node, list):
        return [_convert(item) for item in node]
    if not isinstance(node, dict):
        return node
    node = {key: _convert(value) for key, value in node.items()}

    for key in ("anyOf", "oneOf"):
        variants = node.get(key)
        if isinstance(variants, list) and any(v == _NULL or v == {"nullable": True} for v in variants):
            rest = [v for v in variants if v != _NULL and v != {"nullable": True}]
            del node[key]
            if len(rest) == 1 and "$ref" not in rest[0]:
                node = {**rest[0], **node}
            elif len(rest) == 1:
                node["allOf"] = rest  # a $ref cannot carry sibling keywords in 3.0
            elif rest:
                node[key] = rest
            node["nullable"] = True
    if isinstance(node.get("type"), list):
        types = [t for t in node["type"] if t != "null"]
        if len(types) != len(node["type"]):
            node["nullable"] = True
        node["type"] = types[0] if types else "string"
    if node.get("type") == "null":
        node.pop("type")
        node["nullable"] = True
    if "const" in node:
        node["enum"] = [node.pop("const")]
    if isinstance(node.get("examples"), list) and "type" in node:
        examples = node.pop("examples")
        if examples:
            node["example"] = examples[0]
    for bound, plain in (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")):
        value = node.get(bound)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            node[plain] = value
            node[bound] = True
    if "contentMediaType" in node:
        node.pop("contentMediaType")
        node["format"] = "binary"
    return node


def downgrade_to_openapi_30(schema: dict) -> dict:
    converted = _convert(deepcopy(schema))
    converted["openapi"] = "3.0.3"
    return converted
