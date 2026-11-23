"""The Unified Namespace contract as machine-readable JSON Schema files (Wave 6e, ADR 0021).

``docs/UNS_CONTRACT.md`` explains the contract to people; the files in ``contract/uns/v1/`` state it
for machines. One file per message type refines the shared envelope (``envelope.schema.json``). This
module loads them and checks a message against the schema it names, so that:

* CI fails when anything the ERP publishes drifts from the contract (``tests/test_uns_contract.py``);
* the MES repository can pin the same files (by their ``SHA256SUMS``) and run the same check on what
  it publishes, so neither side can change the contract alone;
* an engineer can check a payload captured from the broker: ``manage.py uns_contract --message f.json``.

The ERP's listener stays a tolerant reader: it accepts what it can post (a quantity sent as a JSON
number, say) and answers anything else with REJECTED and a reason. The schemas are what every
publisher must send; consumers are not made stricter by them, so a schema change cannot stop
production.

The schemas reference each other by ``$id``; a ``referencing`` registry resolves those references
from the files on disk, so nothing is ever fetched from the network.
"""
import hashlib
import json
import logging
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

logger = logging.getLogger(__name__)

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contract" / "uns" / "v1"
EXAMPLES_DIR = CONTRACT_DIR / "examples"
SUMS_FILE = CONTRACT_DIR / "SHA256SUMS"
ENVELOPE = "envelope"


@lru_cache(maxsize=1)
def _load() -> tuple[dict, Registry]:
    """Every schema file by message name (``equipment``...) and a registry of all of them by ``$id``."""
    schemas, resources = {}, []
    for path in sorted(CONTRACT_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schemas[path.name.removesuffix(".schema.json")] = schema
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return schemas, Registry().with_resources(resources)


def schema_files() -> dict:
    """The contract's schemas by message name, envelope included (parsed JSON, not to be modified)."""
    return _load()[0]


def message_types() -> list[str]:
    """The message types the contract defines, as named in an envelope's ``schema``: ``equipment/v1``..."""
    return sorted(schema["properties"]["schema"]["const"] for name, schema in schema_files().items()
                  if name != ENVELOPE)


def _validator(message_type: str) -> Draft202012Validator:
    schemas, registry = _load()
    name, _, _version = message_type.partition("/")
    schema = schemas.get(name)
    if name == ENVELOPE or schema is None or schema["properties"]["schema"]["const"] != message_type:
        raise KeyError(message_type)
    return Draft202012Validator(schema, registry=registry, format_checker=Draft202012Validator.FORMAT_CHECKER)


def errors(message) -> list[str]:
    """Why a message does not follow the contract, one line per problem; empty when it does.

    ``message`` is a parsed envelope, or the payload as bytes or text. An empty payload clears a
    retained topic and is always valid. A message whose ``schema`` the contract does not define is
    reported as such: the contract says consumers ignore it, but a publisher must not send it.
    """
    if isinstance(message, (bytes, str)):
        if not message:
            return []
        try:
            message = json.loads(message)
        except (ValueError, UnicodeDecodeError):
            return ["The payload is not JSON."]
    if not isinstance(message, dict):
        return ["The payload is not a JSON object."]
    message_type = message.get("schema")
    try:
        validator = _validator(str(message_type))
    except KeyError:
        return [f"Unknown schema {message_type!r}; the contract defines {', '.join(message_types())}."]
    found = sorted(validator.iter_errors(message), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(message)'}: {e.message}" for e in found]


def documented_fields(message_type: str) -> set[str]:
    """The ``data`` fields a message type documents: a publisher sends no others."""
    name = message_type.partition("/")[0]
    return set(schema_files()[name]["properties"]["data"]["properties"])


def checksums() -> str:
    """The ``SHA256SUMS`` text of the contract files, as ``sha256sum`` writes it (sorted, forward slashes).

    The MES pins these: a changed schema changes this file, so a contract change is always visible
    in a pull request and deliberate on both sides.
    """
    lines = []
    for path in sorted(p for p in CONTRACT_DIR.rglob("*.json")):
        digest = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        lines.append(f"{digest}  {path.relative_to(CONTRACT_DIR).as_posix()}")
    return "\n".join(lines) + "\n"
