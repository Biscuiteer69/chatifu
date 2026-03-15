#!/bin/bash
echo "=== Preliminary Credential Audit ==="
echo "Looking for .env files in workspace..."
find . -name ".env" -type f
echo "Looking for common key files (.pem, id_rsa, etc)..."
find . \( -name "*.pem" -o -name "id_rsa*" -o -name "*.key" \) -type f
echo "Checking for AWS credentials..."
ls -l ~/.aws/credentials 2>/dev/null || echo "No ~/.aws/credentials found."
echo "=== Audit Complete ==="
