"""Load the safe command catalog (catalog/commands.yml).

The loader returns the plain dict shape that Task 2's CommandIntent validator and
validate_command_args already consume: {command_id: spec}. One source of truth, two
enforcement points (contract boundary + renderer).
"""
from pathlib import Path
from pydantic import BaseModel

from incidentiq.state import validate_command_args

import yaml

# repo-root/catalog/commands.yml, resolved relative to this file (incidentiq/catalog.py)
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent.parent / "catalog" / "commands.yml"


def load_catalog(path: Path | str = DEFAULT_CATALOG_PATH) -> dict:
    """Parse commands.yml → {command_id: spec}. Uses safe_load, never load."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["commands"]


class CatalogError(ValueError):
    """Raised when an intent cannot be safely rendered against the catalog."""


class RenderedCommand(BaseModel):
    command_id: str
    rendered: str                 # the audited string; safety came from validation, not escaping
    args: dict                    # effective args (intent args + filled defaults)
    approval_required: bool


def _with_defaults(args: dict, arg_schema: dict) -> dict:
    """Fill declared defaults so every template placeholder is present at render time."""
    effective = dict(args)
    for name, spec in arg_schema.items():
        if name not in effective and "default" in spec:
            effective[name] = spec["default"]
    return effective


def render_command(command_id: str, args: dict, catalog: dict) -> RenderedCommand:
    """Deterministically render an approved-shape intent into an audited command string.

    No LLM. Re-validates against the catalog (defense in depth — the CommandIntent contract
    already validated once) and refuses anything it can't prove is safe.
    """
    spec = catalog.get(command_id)
    if spec is None:                                       # rule 1: command_id must exist
        raise CatalogError(f"command_id {command_id!r} is not in the catalog")

    arg_schema = spec.get("args", {})
    effective = _with_defaults(args, arg_schema)

    errors = validate_command_args(effective, arg_schema)  # rule 2: type/enum/pattern
    if errors:
        raise CatalogError("; ".join(errors))

    allowed = spec.get("allowed_namespaces")               # rule 3: namespace allowlist
    if "namespace" in effective and allowed is not None and effective["namespace"] not in allowed:
        raise CatalogError(
            f"namespace {effective['namespace']!r} not in allowed_namespaces {allowed}"
        )

    try:                                                   # rule 4: safe substitution
        rendered = spec["template"].format_map(effective)
    except KeyError as e:
        raise CatalogError(f"template references unknown placeholder {e}") from e

    return RenderedCommand(
        command_id=command_id,
        rendered=rendered,
        args=effective,
        approval_required=spec.get("approval_required", True),  # rule 5: surfaced, gated at exec
    )