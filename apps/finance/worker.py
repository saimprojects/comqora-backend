import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from django.db import close_old_connections
from django.utils import timezone

from .documents import draft_review, layout_signature
from .models import SettlementImport, SettlementMapping


def run_document(blob, page=None, filename="", courier_name=""):
    command = [sys.executable, "-m", "apps.finance.document_job", "preview" if page else "extract"]
    if page:
        command.append(str(page))
    else:
        command.extend([str(filename)[:200], str(courier_name)[:120]])
    try:
        result = subprocess.run(
            command,
            input=blob,
            capture_output=True,
            cwd=Path(__file__).resolve().parents[2],
            timeout=20 if page else 55,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            "Statement processing timed out. Split the document into smaller statements or use a fresh export."
        ) from exc
    if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
        raise ValueError(
            "The statement could not be read safely. Use an original PDF, Excel or CSV export within the size limit."
        )
    return result.stdout if page else json.loads(result.stdout)


def process_imports():
    """One independently leased statement per pass; retries require a user action."""
    close_old_connections()
    try:
        now = timezone.now()
        SettlementImport.objects.filter(
            status="PROCESSING", processing_at__lt=now - timedelta(minutes=3)
        ).update(
            status="ERROR",
            error="Processing was interrupted. Retry the import.",
            processing_token=None,
        )
        pending = (
            SettlementImport.objects.filter(status="QUEUED")
            .order_by("created_at")
            .values_list("pk", flat=True)
            .first()
        )
        if not pending:
            return 0
        token = uuid4()
        claimed = SettlementImport.objects.filter(pk=pending, status="QUEUED").update(
            status="PROCESSING", processing_token=token, processing_at=now
        )
        if not claimed:
            return 0
        statement = SettlementImport.objects.get(pk=pending)
        try:
            extracted = run_document(
                bytes(statement.source),
                filename=statement.filename,
                courier_name=statement.courier.name,
            )
            # Work on a copy: original parser evidence remains unchanged.
            import copy

            review = draft_review(copy.deepcopy(extracted))
            mappings = {
                m.signature: m.roles
                for m in SettlementMapping.objects.filter(
                    workspace_id=statement.workspace_id,
                    courier_id=statement.courier_id,
                    statement__status="CONFIRMED",
                )
            }
            for table in review["tables"]:
                roles = mappings.get(layout_signature(table["columns"]))
                if roles and len(roles) == len(table["columns"]):
                    for column, role in zip(table["columns"], roles):
                        column["role"] = role
                    extracted["warnings"].append(
                        "An identical heading layout used your workspace's previously approved column mapping. Verify it still applies to this statement."
                    )
            SettlementImport.objects.filter(
                pk=pending, processing_token=token, status="PROCESSING"
            ).update(
                status="REVIEW",
                extracted=extracted,
                review=review,
                error="",
                processing_token=None,
            )
        except Exception:
            SettlementImport.objects.filter(
                pk=pending, processing_token=token, status="PROCESSING"
            ).update(
                status="ERROR",
                error="Statement extraction failed or timed out. Retry with an original PDF, Excel or CSV export (up to 8 MB); scanned PDFs may need manual transcription.",
                processing_token=None,
            )
        return 1
    finally:
        close_old_connections()
