"""Run the mock target app:  python -m target_app --tenant acme --port 5001"""
from __future__ import annotations

import argparse

from .app import create_app
from .faults import FaultConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock legacy core-banking teller app")
    parser.add_argument("--tenant", default="acme", help="tenant config in target_app/tenants (acme, riverbend)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible fault injection")
    parser.add_argument("--interstitial-rate", type=float, default=0.0)
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--error-rate", type=float, default=0.0)
    parser.add_argument("--session-ttl", type=int, default=900)
    parser.add_argument("--no-confirm-dialog", action="store_true")
    args = parser.parse_args()

    faults = FaultConfig()
    faults.update({
        "interstitial_rate": args.interstitial_rate,
        "delay_ms": args.delay_ms,
        "error_rate": args.error_rate,
        "session_ttl_s": args.session_ttl,
        "confirm_dialog": not args.no_confirm_dialog,
    })
    app = create_app(tenant=args.tenant, faults=faults, seed=args.seed)
    print(f"CoreLine Teller mock [{args.tenant}] on http://{args.host}:{args.port}  faults={faults.to_dict()}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
