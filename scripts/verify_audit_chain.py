"""Independent audit-chain verifier (read-only).
  python scripts/verify_audit_chain.py --database-url-env DATABASE_URL   # reads chained rows with SELECT only
  python scripts/verify_audit_chain.py --json export.json                # list of entries
Options: --expect-head <sha256>  --expect-count N   (external anchors; needed to detect deletion of the newest rows)
Exit code 0 = chain intact, 1 = tampering/inconsistency detected, 2 = could not read."""
import argparse, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.core.audit_chain import verify_chain


def load(args):
    if args.json:
        return json.loads(Path(args.json).read_text(encoding="utf-8"))
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ[args.database_url_env], row_factory=dict_row) as conn:  # SELECT only; no writes
        rows = conn.execute("""SELECT seq, ts_signed AS ts, actor, event AS action, resource_id, correlation_id,
            detail, prev_hash, hash, canonical FROM audit_log WHERE seq IS NOT NULL ORDER BY seq""").fetchall()
    return [dict(r) for r in rows]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json"); ap.add_argument("--database-url-env", default="DATABASE_URL")
    ap.add_argument("--expect-head"); ap.add_argument("--expect-count", type=int)
    a = ap.parse_args(argv)
    try: entries = load(a)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"could not read audit entries: {type(exc).__name__}"})); return 2
    report = verify_chain(entries, expect_head=a.expect_head, expect_count=a.expect_count)
    print(json.dumps(report, indent=2)); return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
