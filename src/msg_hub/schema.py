"""精简 JSON Schema 校验器（零依赖）。

只支持本仓库 manifest.json 用到的子集：
    type / required / properties / items / enum / anyOf
    minLength / minItems / minimum / maximum / default
    （title / description / secret / examples 等展示字段原样保留）

为什么不用 jsonschema 库：stdio 本地版要能在 pi-box 容器（仅 python3，无 pip）
里直接运行，核心不允许第三方依赖。
"""

from __future__ import annotations

from typing import Any

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
# bool 是 int 的子类，需要显式排除
_TYPE_EXCLUDE = {"integer": bool, "number": bool}


class SchemaError(Exception):
    """schema 本身不合法（manifest 写错了）。"""


def _check_type(value: Any, expected: str) -> bool:
    if expected not in _TYPES:
        raise SchemaError(f"不支持的 type：{expected}")
    if isinstance(value, _TYPES[expected]) and not isinstance(value, _TYPE_EXCLUDE.get(expected, ())):
        if expected == "integer" and isinstance(value, float):
            return False
        return True
    return False


def _validate(value: Any, schema: dict[str, Any], path: str, errors: list[tuple[str, str]]) -> bool:
    """返回是否通过本 schema；把错误追加到 errors（相对 path 的字段名 + 说明）。"""
    if not isinstance(schema, dict):
        raise SchemaError("schema 节点必须是对象")

    if "anyOf" in schema:
        branches = schema["anyOf"]
        if not isinstance(branches, list) or not branches:
            raise SchemaError("anyOf 必须是非空数组")
        branch_errors: list[str] = []
        for branch in branches:
            probe: list[tuple[str, str]] = []
            if _validate(value, branch, path, probe):
                return True
            branch_errors.append("; ".join(f"{field}: {message}" for field, message in probe))
        errors.append((path or "(root)", f"不满足任一分支（anyOf）：{' / '.join(branch_errors)}"))
        return False

    expected_type = schema.get("type")
    if expected_type is not None and not _check_type(value, str(expected_type)):
        errors.append((path or "(root)", f"应为 {expected_type} 类型，实际 {type(value).__name__}"))
        return False

    ok = True
    if "enum" in schema:
        if value not in schema["enum"]:
            errors.append((path or "(root)", f"必须是 {schema['enum']} 之一"))
            ok = False

    if isinstance(value, str) and "minLength" in schema and len(value) < int(schema["minLength"]):
        errors.append((path or "(root)", f"长度不足（最少 {schema['minLength']}）"))
        ok = False

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append((path or "(root)", f"小于最小值 {schema['minimum']}"))
            ok = False
        if "maximum" in schema and value > schema["maximum"]:
            errors.append((path or "(root)", f"大于最大值 {schema['maximum']}"))
            ok = False

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append((path or "(root)", f"元素不足（最少 {schema['minItems']}）"))
            ok = False
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                if not _validate(item, items, f"{path}.{index}" if path else str(index), errors):
                    ok = False

    if isinstance(value, dict):  # required/properties 对 dict 实例生效（分支 schema 可能省略 type）
        required = schema.get("required") or []
        if not isinstance(required, list):
            raise SchemaError("required 必须是数组")
        for name in required:
            if name not in value:
                errors.append((name if not path else f"{path}.{name}", f"必填字段缺失（{name}）"))
                ok = False
        properties = schema.get("properties") or {}
        if not isinstance(properties, dict):
            raise SchemaError("properties 必须是对象")
        for name, prop in properties.items():
            if name in value and isinstance(prop, dict):
                child_path = name if not path else f"{path}.{name}"
                if not _validate(value[name], prop, child_path, errors):
                    ok = False

    return ok


def validate(schema: dict[str, Any], value: Any) -> list[dict[str, str]]:
    """返回错误列表 [{field, message}]；为空即通过。"""
    errors: list[tuple[str, str]] = []
    _validate(value, schema, "", errors)
    return [{"field": field, "message": message} for field, message in errors]
