# Protocol contracts

`v1/contracts.json` is the machine-readable contract used by the protocol gate.
It freezes required fields, primitive types, and V1 enums without introducing a
runtime dependency on a JSON Schema package.

Every event carries a public `streamId`. Sequence numbers are contiguous only
inside that stream; a changed stream identity invalidates the old cursor and
forces `sync.required` plus a fresh snapshot.

Unknown rich-message blocks are intentionally not rejected. They are accepted
only when they carry readable `fallbackText`, matching the mobile degradation
rule. Unknown event types remain rejected because event mutation semantics must
be known by the reducer.

Run the gate with:

```sh
python3 tools/run_protocol_gate.py
```
