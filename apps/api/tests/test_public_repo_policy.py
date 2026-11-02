from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from scripts.security.public_repo_policy import (  # noqa: E402
    collect_tracked_paths,
    find_content_violations,
    find_path_violations,
)


def test_public_readmes_are_allowed() -> None:
    violations = find_path_violations(
        [
            "README.md",
            "README.en.md",
            "README.zh-CN.md",
            "README.ja.md",
            "README.vi.md",
            "README.fr.md",
            "README.de.md",
        ]
    )

    assert violations == []


def test_internal_documents_are_rejected() -> None:
    violations = find_path_violations(
        [
            "README.es.md",
            "docs/testing/report.md",
            "docs/manual/cga-user-manual/README.ko.md",
        ]
    )

    assert "README.es.md:non-manual-document" in violations
    assert "docs/testing/report.md:non-manual-document" in violations
    assert all(
        "docs/manual/cga-user-manual/README.ko.md" not in item
        for item in violations
    )


def test_sensitive_values_are_reported_without_value(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sensitive_value = "cy" + "huh"
    sample.write_text(f'owner = "{sensitive_value}"\n', encoding="utf-8")

    violations = find_content_violations(tmp_path, ["sample.py"])

    assert violations == ["sample.py:1:personal-identifier"]
    assert sensitive_value not in violations[0]


def test_reserved_example_domains_are_allowed(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        'primary = "qa@example.com"\nsecondary = "qa@example.invalid"\n',
        encoding="utf-8",
    )

    assert find_content_violations(tmp_path, ["sample.py"]) == []


def test_database_dsn_is_not_reported_as_an_email(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        'dsn = "postgresql://db_user@127.0.0.1:5432/app"\n',
        encoding="utf-8",
    )

    assert find_content_violations(tmp_path, ["sample.py"]) == []


def test_current_git_index_has_no_private_documents() -> None:
    paths = collect_tracked_paths(REPO_ROOT)

    assert find_path_violations(paths) == []


def test_public_fixtures_have_no_personal_identifiers() -> None:
    paths = (
        "compat-samples/Aidot 봇_v1.json",
        "compat-samples/dialog-flow-Rich Form.json",
        "apps/api/tests/integration/test_bot_persistence_regression.py",
        "apps/api/tests/test_admin_operations_dashboard.py",
        "apps/api/tests/test_llm_intent.py",
        "apps/web/lib/entity-assets.ts",
    )

    assert find_content_violations(REPO_ROOT, paths) == []


def test_current_tracked_text_has_no_sensitive_identifiers() -> None:
    paths = collect_tracked_paths(REPO_ROOT)

    assert find_content_violations(REPO_ROOT, paths) == []


def test_secret_scan_workflow_is_immutable_and_full_history() -> None:
    workflow = (REPO_ROOT / ".github/workflows/secret-scan.yml").read_text(
        encoding="utf-8"
    )

    assert "3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "fetch-depth: 0" in workflow
    assert "GITLEAKS_VERSION: 8.30.1" in workflow
    assert "061476c21adaf5441516f96f185c1a4706a83cd6329b9b38762271b3d4a52fae" in workflow
    assert "gitleaks git --redact" in workflow
    assert "python scripts/security/public_repo_policy.py --repo ." in workflow


def test_office_and_pdf_manuals_are_treated_as_binary() -> None:
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "*.docx binary" in attributes
    assert "*.pdf binary" in attributes



def test_exact_public_google_chat_issuer_is_allowed(tmp_path):
    sample=tmp_path/'sample.py'
    sample.write_text('issuer = "chat@system.gserviceaccount.com"',encoding='utf-8')
    assert find_content_violations(tmp_path,['sample.py'])==[]


def test_other_google_like_addresses_remain_private(tmp_path):
    addresses=[('other','system.gserviceaccount.com'),('chat','project.iam.gserviceaccount.com'),('chat','system.gserviceaccount.com.attacker.com'),('notchat','system.gserviceaccount.com'),('CHAT','system.gserviceaccount.com')]
    sample=tmp_path/'sample.py'
    for local,domain in addresses:
        sample.write_text(f'email = "{local}@{domain}"',encoding='utf-8')
        assert find_content_violations(tmp_path,['sample.py'])==['sample.py:1:personal-email']


def test_public_google_issuer_does_not_hide_mixed_private_email(tmp_path):
    sample=tmp_path/'sample.py';local='private';domain='unapproved.test'
    sample.write_text(f'issuer = "chat@system.gserviceaccount.com"; email = "{local}@{domain}"',encoding='utf-8')
    assert find_content_violations(tmp_path,['sample.py'])==['sample.py:1:personal-email']
