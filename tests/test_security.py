"""
Security and Credential Sanitization Unit Tests.
Asserts that no secrets or private keys are exposed in source code, logs, or error strings.
Verifies .gitignore rules and placeholder templates.
"""

import os
import re
import pytest
from pathlib import Path


def test_gitignore_protects_secrets():
    gitignore_path = Path(".gitignore")
    assert gitignore_path.is_file(), ".gitignore file must exist"
    
    content = gitignore_path.read_text(encoding="utf-8")
    required_patterns = [".env", "*.pem", "*.key", "logs/"]
    for pat in required_patterns:
        assert pat in content, f".gitignore must contain '{pat}' to protect secrets"


def test_no_hardcoded_secrets_in_source():
    """Scan all python files in src/ and config/ for accidentally committed secrets."""
    base_dir = Path(".")
    py_files = list(base_dir.glob("src/**/*.py")) + list(base_dir.glob("config/**/*.py"))
    
    # Patterns for private keys and common API key formats
    secret_patterns = [
        re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----"),
        re.compile(r"0x[a-fA-F0-9]{64}"),  # 32-byte Ethereum private key hex
        re.compile(r"(?:api_secret|private_key)\s*=\s*['\"][a-zA-Z0-9_\-]{20,}['\"]"),
    ]

    for fpath in py_files:
        text = fpath.read_text(encoding="utf-8")
        for pat in secret_patterns:
            matches = pat.findall(text)
            assert len(matches) == 0, f"Found potential secret in {fpath}: {matches}"


def test_env_example_has_only_placeholders():
    example_path = Path("config/.env.example")
    if not example_path.is_file():
        example_path = Path(".env.example")
        
    assert example_path.is_file(), ".env.example must exist"
    content = example_path.read_text(encoding="utf-8")
    
    # Ensure no live private keys exist in example file
    assert "-----BEGIN" not in content
    assert "0x" not in content or "0x000" in content or "your_" in content.lower()
    for line in content.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            v_clean = v.strip().strip("'\"")
            assert (
                "your_" in v_clean.lower()
                or "here" in v_clean.lower()
                or "placeholder" in v_clean.lower()
                or v_clean == ""
            ), f"Key {k} has non-placeholder value '{v_clean}' in .env.example"
