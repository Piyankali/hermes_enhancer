"""Child-process driver for v0.22 abnormal-termination crash tests.

Each mode performs a precise crash-point sequence against an isolated
tmp_path database, then dies via os._exit() WITHOUT graceful shutdown
(no flush, no close, no worker drain). The parent test process asserts
the abnormal exit code, then starts a fresh FederatedDB whose automatic
startup recovery must restore durability invariants.

Usage: python crash_child.py <db_path> <mode> [args...]

Modes:
  buffer_only <event_id>
      enqueue_to_buffer() + COMMIT, then die. Final persistence never ran.
  push_no_finalize <event_id>
      push() with mark_buffer_persisted monkeypatched to os._exit():
      buffer commit + final INSERT commit happen, finalization never runs.
  bulk <count> <prefix>
      enqueue <count> durable buffer events, then die.
  recover_then_die
      normal FederatedDB init (automatic startup recovery runs), then die
      without shutdown.
"""
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from federated_db import FederatedDB


def main():
    db_path = sys.argv[1]
    mode = sys.argv[2]

    if mode == "buffer_only":
        event_id = sys.argv[3]
        db = FederatedDB(db_path=db_path)
        ok = db.enqueue_to_buffer(
            event_id, "trace-%s" % event_id, "call-%s" % event_id,
            "pre_tool_call", {"tool": "crash_test"},
        )
        if not ok:
            os._exit(10)
        os._exit(99)

    if mode == "push_no_finalize":
        event_id = sys.argv[3]
        db = FederatedDB(db_path=db_path)

        def _die(_eid):
            os._exit(88)

        db.mark_buffer_persisted = _die
        try:
            db.push({
                "tool": "crash_test",
                "event_id": event_id,
                "trace_id": "trace-%s" % event_id,
                "tool_call_id": "call-%s" % event_id,
            })
        except Exception:
            os._exit(4)
        os._exit(3)  # should be unreachable: _die fires first

    if mode == "bulk":
        count = int(sys.argv[3])
        prefix = sys.argv[4]
        db = FederatedDB(db_path=db_path)
        for i in range(count):
            eid = "%s-%d" % (prefix, i)
            ok = db.enqueue_to_buffer(
                eid, "trace-%s" % eid, "call-%s" % eid,
                "pre_tool_call", {"tool": "crash_test"},
            )
            if not ok:
                os._exit(11)
        os._exit(99)

    if mode == "recover_then_die":
        db = FederatedDB(db_path=db_path)
        # __init__ already ran automatic startup recovery synchronously.
        os._exit(77)

    if mode == "push_bulk":
        count = int(sys.argv[3])
        prefix = sys.argv[4]
        import uuid as _uuid
        db = FederatedDB(db_path=db_path)
        for i in range(count):
            eid = _uuid.uuid5(_uuid.NAMESPACE_URL, "%s-%d" % (prefix, i)).hex
            db.push({
                "tool": "mp_stress",
                "event_id": eid,
                "trace_id": "trace-%s" % eid,
                "tool_call_id": "call-%s" % eid,
            })
        db.shutdown()
        os._exit(0)

    os._exit(2)


if __name__ == "__main__":
    main()
