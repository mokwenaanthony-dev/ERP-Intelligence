# The UNS contract, as files

The machine-readable form of [`docs/UNS_CONTRACT.md`](../../docs/UNS_CONTRACT.md), the agreement
between ERP-Intelligence and [MES-Intelligence](https://github.com/mokwenaanthony-dev/MES-Intelligence)
([ADR 0021](../../docs/decisions/0021-uns-contract-as-json-schema-files.md)). The document explains
the contract; these files are what CI checks.

```
contract/uns/v1/
  envelope.schema.json               the envelope every message shares, and the shared types
  equipment.schema.json              equipment/v1               ERP -> retained <cell>/definition/equipment
  operation-request.schema.json      operation-request/v1       ERP -> retained <cell>/schedule/operation-requests/...
  operation-performance.schema.json  operation-performance/v1   MES -> <cell>/events/operation-performance
  confirmation-result.schema.json    confirmation-result/v1     ERP -> <company>/<plant>/erp/events/confirmation-result
  equipment-state.schema.json        equipment-state/v1         MES or edge -> retained <cell>/state/status
  examples/                          one complete message per type
  SHA256SUMS                         the checksums another repository pins
```

JSON Schema draft 2020-12. The schemas reference each other by `$id` (an address under this
repository on GitHub); resolve those from the files, as `apps/uns/schemas.py` does, rather than over
the network.

## Checking

```bash
python manage.py uns_contract                      # check the examples; rewrite SHA256SUMS after a change
python manage.py uns_contract --check              # the same, but fail if SHA256SUMS is out of date
python manage.py uns_contract --message f.json     # check one payload, e.g. captured with mosquitto_sub
```

In Docker, from Git Bash: copy the file in with `MSYS_NO_PATHCONV=1 docker compose cp f.json web:/tmp/f.json`,
then `docker compose exec web sh -c "python manage.py uns_contract --message /tmp/f.json"` (Git Bash
rewrites arguments that start with `/` into Windows paths; inside `sh -c` they are left alone).

## Using it from another repository

Copy the `v1` folder, keep it unchanged, and check it against the `SHA256SUMS` of the ERP release you
implement. Then validate every message your side publishes in your own tests. Line endings are
normalised to `\n` before hashing, so check with the same rule (Git for Windows may check files out
with `\r\n`):

```python
import hashlib, pathlib
for line in pathlib.Path("v1/SHA256SUMS").read_text().splitlines():
    digest, name = line.split("  ", 1)
    data = pathlib.Path("v1", name).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == digest, f"{name} differs from the pinned contract"
```

## Changing it

Adding an optional field is not a new version: add it to the schema and an example, run
`python manage.py uns_contract`, and commit the new `SHA256SUMS`. Removing or renaming a field, or
changing its meaning, is a new version: a new `v2` folder beside `v1`, and publishers send both until
every consumer has moved (contract section 5).
