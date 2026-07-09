"""HCL parser — narrow, hand-rolled. Parses the subset of HCL we emit.

Scope (intentionally tiny):
  - `locals { ... }` blocks
  - string / number / list literals
  - `${local.X}` and `${var.X}` interpolations (resolved against
    the same file's `variable "X" { default = "..." }` blocks)
  - nested map-of-objects (one level only, used for the cicd/apps
    cluster roots)

Why not `python-hcl2`? Two reasons:
  1. We control the cluster-root shape — the orchestrator writes
     these files, so we know they only use a tiny subset of HCL.
  2. `python-hcl2` adds a third-party dep with a 5MB+ transitive
     tree. The cicd repo's tools/lib/ uses the same regex approach
     and we vendor that pattern 1:1 here.

Public API:
  HclParseError                -- raised when main.tf can't be parsed
  parse_cluster_root(path)     -- returns ClusterIntent (typed)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class HclParseError(ValueError):
    """Raised when main.tf can't be parsed into a ClusterIntent."""


# Strip `# ...` and `// ...` line comments. We don't try to handle
# `/* ... */` block comments (they don't appear in the cluster root
# templates).
def _strip_hcl_comments(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines():
        in_string = False
        escape = False
        cut_at: int | None = None
        for i, ch in enumerate(line):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if not in_string and ch == "#":
                cut_at = i
                break
        if cut_at is not None:
            line = line[:cut_at]
        out.append(line)
    return "\n".join(out)


def _extract_block(text: str, header_re: str) -> str | None:
    """Return the inner body of the first block matching `header_re`.

    Tracks brace depth so nested `{ ... }` are captured. Returns
    None if no block is found.
    """
    match = re.search(header_re, text)
    if not match:
        return None
    start = match.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i]
        i += 1
    return None


def _parse_string_list(text: str) -> list[str]:
    """Parse a HCL list of string literals, e.g. [\"a\", \"b\"]."""
    out: list[str] = []
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', text):
        out.append(m.group(1).encode("utf-8").decode("unicode_escape"))
    return out


# ----------------------------------------------------------------- model


@dataclass(frozen=True)
class ClusterIntent:
    """The typed view of `infra/clusters/<name>/main.tf`.

    Mirrors the static fields every cluster root must declare. The
    orchestrator uses this as the *desired state* against which the
    live cluster is diffed.

    `raw_locals` is the un-parsed locals block so callers can read
    fields we don't model explicitly (e.g. `tags`).
    """

    cluster_name: str
    pod_cidr: str
    svc_cidr: str
    cluster_dns: str
    k3s_version: str
    cf_tunnel_name: str
    csi_storage: str
    ccm_region: str
    ccm_zone: str
    install_k3s_exec_server: tuple[str, ...]
    install_k3s_exec_agent: tuple[str, ...]
    # Anything in `locals` that we don't model explicitly. Used by
    # the orchestrator's "extra fields" path (e.g. tests assert
    # these stay stable).
    raw_locals: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------- parser core


_VAR_RE = re.compile(r"\$\{local\.(\w+)\}|\$\{var\.(\w+)\}")


def _resolve_string(
    raw: str,
    locals_map: dict[str, Any],
    variables_default: dict[str, str],
) -> str:
    """Resolve ${local.X} and ${var.X} interpolations inside a string.

    Resolution order: locals first, then variable defaults. We do
    not support expressions or functions (the cluster root templates
    never use them).
    """

    def _sub(match: re.Match[str]) -> str:
        local_name = match.group(1)
        var_name = match.group(2)
        if local_name:
            if local_name not in locals_map:
                raise HclParseError(f"local.{local_name} referenced but not defined")
            value = locals_map[local_name]
            if not isinstance(value, str):
                raise HclParseError(
                    f"local.{local_name} interpolated as string but is {type(value).__name__}"
                )
            return value
        if var_name:
            if var_name not in variables_default:
                raise HclParseError(
                    f"var.{var_name} referenced but no `variable {var_name!r} {{ default = ... }}` block"
                )
            return variables_default[var_name]
        raise HclParseError(f"unhandled interpolation: {match.group(0)!r}")

    return _VAR_RE.sub(_sub, raw)


def _parse_string_value(raw: str, locals_map: dict[str, Any], variables_default: dict[str, str]) -> str:
    """Parse a HCL string literal, resolving interpolations."""
    raw = raw.strip()
    if not (raw.startswith('"') and raw.endswith('"')):
        raise HclParseError(f"expected string literal, got: {raw!r}")
    inner = raw[1:-1]
    inner = inner.encode("utf-8").decode("unicode_escape", errors="replace")
    return _resolve_string(inner, locals_map, variables_default)


def _parse_number_value(raw: str) -> int:
    raw = raw.strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise HclParseError(f"expected number literal, got: {raw!r}") from exc


def _parse_list_of_strings(
    raw: str, locals_map: dict[str, Any], variables_default: dict[str, str]
) -> tuple[str, ...]:
    """Parse `[ "--foo", "--bar=${local.x}" ]` etc."""
    raw = raw.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        raise HclParseError(f"expected list literal, got: {raw!r}")
    body = raw[1:-1].strip()
    if not body:
        return ()
    # Re-wrap each captured string back in quotes so _parse_string_value's
    # "starts and ends with quote" precondition holds.
    out: list[str] = []
    for m in re.finditer(r'"([^"]*(?:\\"[^"]*)*)"', body):
        out.append(_parse_string_value(f'"{m.group(1)}"', locals_map, variables_default))
    return tuple(out)


def _parse_variable_defaults(text: str) -> dict[str, str]:
    """Walk every `variable "X" { default = "..." }` block and return defaults."""
    defaults: dict[str, str] = {}
    # We re-extract each variable block individually.
    for match in re.finditer(r'\bvariable\s+"([^"]+)"\s*\{', text):
        name = match.group(1)
        body = _extract_block(text, re.escape(match.group(0)))
        if body is None:
            continue
        # default can be a string, number, or bool. We only need string defaults
        # (variables like ssh_proxy_port are numbers; the orchestrator uses
        # safe built-in defaults for those).
        default_match = re.search(r'default\s*=\s*("([^"\\]|\\.)*")', body)
        if default_match:
            raw_default = default_match.group(1)
            defaults[name] = raw_default[1:-1].encode("utf-8").decode("unicode_escape", errors="replace")
    return defaults


def _parse_locals(text: str, variables_default: dict[str, str]) -> dict[str, Any]:
    """Parse the `locals { ... }` block into a typed dict.

    We handle:
      - string scalars (with interpolation resolution)
      - number scalars
      - list-of-string scalars (e.g. install_k3s_exec_server)

    Anything else (maps, conditionals, for-loops) is unsupported
    and raises HclParseError. The cicd/apps cluster roots only use
    these three shapes.
    """
    body = _extract_block(text, r"\blocals\s*\{")
    if body is None:
        raise HclParseError("no `locals { ... }` block found in main.tf")

    # Two-pass parse: scalars + lists first, then second pass to
    # resolve interpolations (so `cluster_name` referenced before
    # its declaration still works).
    raw_assignments: dict[str, str] = {}
    # Match key = value where value is either a single line OR a
    # balanced bracket expression (which may span lines).
    pos = 0
    while pos < len(body):
        m = re.match(r'\s*(\w+)\s*=\s*', body[pos:])
        if not m:
            pos += 1
            continue
        key = m.group(1)
        start = pos + m.end()
        # Try to extract a balanced [ ... ] expression.
        if start < len(body) and body[start] == "[":
            depth = 1
            i = start + 1
            while i < len(body) and depth > 0:
                if body[i] == "[":
                    depth += 1
                elif body[i] == "]":
                    depth -= 1
                elif body[i] == "#":
                    # Skip rest of line (a comment).
                    while i < len(body) and body[i] != "\n":
                        i += 1
                i += 1
            if depth != 0:
                raise HclParseError(f"unterminated list for {key!r}")
            raw_assignments[key] = body[start:i]
            pos = i
            continue
        # Otherwise single line, terminated by `,` or end-of-line.
        end_match = re.search(r'[,\n]', body[start:])
        end = start + (end_match.start() if end_match else len(body) - start)
        raw_assignments[key] = body[start:end].strip()
        pos = end + 1

    parsed: dict[str, Any] = {}
    for key, raw in raw_assignments.items():
        # Bare `${local.X}` or `${var.X}` without surrounding quotes
        # is valid HCL and resolves to a string.
        bare_ref_match = re.fullmatch(r"\$\{(local|var)\.(\w+)\}", raw)
        if bare_ref_match:
            ref_kind, ref_name = bare_ref_match.group(1), bare_ref_match.group(2)
            if ref_kind == "local":
                if ref_name not in parsed:
                    raise HclParseError(f"local.{ref_name} referenced but not defined")
                parsed[key] = parsed[ref_name]
                continue
            # var.X with no surrounding quotes -> use the variable default.
            if ref_name not in variables_default:
                raise HclParseError(
                    f"var.{ref_name} referenced but no `variable {ref_name!r} {{ default = ... }}` block"
                )
            parsed[key] = variables_default[ref_name]
            continue
        if raw.startswith('"'):
            parsed[key] = _parse_string_value(raw, parsed, variables_default)
        elif raw.startswith("["):
            parsed[key] = _parse_list_of_strings(raw, parsed, variables_default)
        elif re.fullmatch(r"-?\d+", raw):
            parsed[key] = _parse_number_value(raw)
        elif raw in ("true", "false"):
            parsed[key] = raw == "true"
        else:
            raise HclParseError(
                f"unsupported locals shape for {key!r}: {raw!r} "
                "(expected string, number, list-of-strings, or bool)"
            )
    return parsed


# ----------------------------------------------------------- public API


_REQUIRED_FIELDS = (
    "cluster_name",
    "pod_cidr",
    "svc_cidr",
    "cluster_dns",
    "k3s_version",
    "cf_tunnel_name",
    "csi_storage",
    "ccm_region",
    "ccm_zone",
    "install_k3s_exec_server",
    "install_k3s_exec_agent",
)


def parse_cluster_root(path: Path) -> ClusterIntent:
    """Read `infra/clusters/<name>/main.tf` and return a ClusterIntent."""
    try:
        text = path.read_text()
    except FileNotFoundError as exc:
        raise HclParseError(f"main.tf not found: {path}") from exc
    text = _strip_hcl_comments(text)
    variables_default = _parse_variable_defaults(text)
    locals_map = _parse_locals(text, variables_default)

    missing = [f for f in _REQUIRED_FIELDS if f not in locals_map]
    if missing:
        raise HclParseError(
            f"cluster root {path.name} is missing required locals keys: {missing}"
        )

    return ClusterIntent(
        cluster_name=locals_map["cluster_name"],
        pod_cidr=locals_map["pod_cidr"],
        svc_cidr=locals_map["svc_cidr"],
        cluster_dns=locals_map["cluster_dns"],
        k3s_version=locals_map["k3s_version"],
        cf_tunnel_name=locals_map["cf_tunnel_name"],
        csi_storage=locals_map["csi_storage"],
        ccm_region=locals_map["ccm_region"],
        ccm_zone=locals_map["ccm_zone"],
        install_k3s_exec_server=tuple(locals_map["install_k3s_exec_server"]),
        install_k3s_exec_agent=tuple(locals_map["install_k3s_exec_agent"]),
        raw_locals=locals_map,
    )
