"""
ReelSeat - a movie booking website running behind a load balancer.

How to run (only Python is needed, nothing to install):
    python run.py

Then open in your browser:
    Website:          http://localhost:8080
    Server panel:     http://localhost:8080/admin
    Owner dashboard:  log in with the details in owner_login.txt, then click "Owner dashboard"

Press Ctrl+C in this window to stop everything.

The files:
    run.py            starts everything (this file)
    loadbalancer.py   the load balancer and its strategies
    app.py            the booking servers (all three run this)
    database.py       the shared database
    static/           the web pages
"""

import os

import database
import loadbalancer

LB_PORT = 8080

if __name__ == "__main__":
    new_owner_password = database.init_db()
    print("Starting servers...")
    for b in loadbalancer.pool.backends:
        b.start()
        print(f"  {b.name} running on port {b.port}")

    print()
    print(f"  Website:          http://localhost:{LB_PORT}")
    print(f"  Server panel:     http://localhost:{LB_PORT}/admin")
    if new_owner_password:
        print()
        print("  Owner account created:")
        print("    Email:    owner@reelseat.local")
        print(f"    Password: {new_owner_password}")
    if os.path.exists(database.OWNER_FILE):
        print(f"  (Owner login is saved in {os.path.basename(database.OWNER_FILE)})")
    print("\n  Press Ctrl+C to stop.\n")
    try:
        loadbalancer.serve(LB_PORT)
    except KeyboardInterrupt:
        print("\nStopped.")
