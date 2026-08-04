#!/usr/bin/env python3
"""
Generates the secret values you need to fill in .env:
  - SECRET_KEY        (session/CSRF signing)
  - ENCRYPTION_KEY     (Fernet key for encrypting the LLM API key at rest)
  - ADMIN_PASSWORD_HASH (bcrypt hash of a password you choose)

Usage:
    python3 scripts/gen_secrets.py
    python3 scripts/gen_secrets.py --password "your-new-admin-password"
"""
import argparse
import getpass
import secrets

import bcrypt
from cryptography.fernet import Fernet


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--password", help="Admin password (omit to be prompted, hidden input)")
    args = parser.parse_args()

    password = args.password or getpass.getpass("Choose an admin password: ")
    if len(password) < 12:
        print("Warning: use a password of at least 12 characters for a production deployment.")

    secret_key = secrets.token_urlsafe(48)
    encryption_key = Fernet.generate_key().decode()
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode()

    print("\nAdd these to your .env file:\n")
    print(f"SECRET_KEY={secret_key}")
    print(f"ENCRYPTION_KEY={encryption_key}")
    print(f"ADMIN_PASSWORD_HASH={password_hash}")
    print("\nKeep .env out of version control and out of any web-served directory.")


if __name__ == "__main__":
    main()
