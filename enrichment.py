"""Webhook event handlers: fetch full file data, parse, write to the enabled backends."""
import os
import logging
import requests
from frameio_client import (
    get_file,
    parse_metadata,
    get_project,
    resolve_version_stack_id,
    get_version_stack_children,
)

logger = logging.getLogger(__name__)

ACCOUNT_ID = os.environ['FRAMEIO_ACCOUNT_ID']

# Backend toggles. Google Sheets is the active path; Airtable is retained but
# disabled by default. Either or both may run.
SHEETS_ENABLED = os.environ.get('SHEETS_ENABLED', 'true').lower() == 'true'
AIRTABLE_ENABLED = os.environ.get('AIRTABLE_ENABLED', 'false').lower() == 'true'

# Statuses that mean an asset has left this project's tracking. When an asset's
# `Overall Video Status` becomes one of these, its sheet row is deleted rather
# than updated. Matched case-insensitively (whitespace ignored).
REMOVAL_STATUSES = ('Full Length Lecture',)

# A row is only deleted on a removal status if its *previous* status (the value
# already in the sheet's Status column) was one of these editing stages. A video
# in R1/R2 Edits that becomes "Full Length Lecture" is too long to keep and will
# be re-uploaded as smaller segments, so its row is removed. From any other prior
# status (e.g. Not Using, Approved) or a blank status, the row is kept and just
# updated with the new status.
DELETABLE_PRIOR_STATUSES = ('R1 Edits', 'R2 Edits')


def _normalize_status(s: str) -> str:
    """Lowercase and strip all whitespace for tolerant status comparison."""
    return "".join(str(s).split()).lower()


_REMOVAL_STATUS_SET = {_normalize_status(s) for s in REMOVAL_STATUSES}

# Frame.io metadata field name → internal key used by airtable_writer.
# Names are matched case-insensitively (see handle_event), so the casing here
# is just for readability.
METADATA_FIELD_MAP = {
    'Overall Video Status': 'status',
    'PM': 'pm',
    'SME': 'sme',
    'Notes': 'notes',
    'Production ID': 'production_id',
    'MODULE': 'module',
    'ID': 'id',
}

# Events that warrant fetching full file + updating Airtable
ENRICHMENT_EVENTS = {
    'file.created',
    'file.ready',
    'file.label.updated',
    'file.versioned',
    'metadata.value.updated',
}


def _extract_file_id(event: dict) -> str:
    """Get the file ID from various possible locations in the webhook payload."""
    resource = event.get('resource') or {}
    return resource.get('id') or event.get('file_id') or ''


def _resolve_project_name(event: dict, file_data: dict) -> str | None:
    """Resolve the Frame.io project name for an asset.

    The project name is used to pick the matching Airtable table. Returns None
    if the project can't be determined, in which case the caller should skip.
    """
    project_id = (
        file_data.get('project_id')
        or (file_data.get('project') or {}).get('id')
        or (event.get('project') or {}).get('id')
        or ((event.get('resource') or {}).get('project') or {}).get('id')
    )
    if not project_id:
        logger.warning("Could not determine project_id from file or event payload")
        return None

    try:
        project = get_project(ACCOUNT_ID, project_id)
    except Exception as e:
        logger.warning(f"Failed to fetch project {project_id}: {e}")
        return None

    name = project.get('name')
    if not name:
        logger.warning(f"Project {project_id} has no name")
    return name


def _newest_child(children: list) -> dict | None:
    """Pick the most recent version in a stack.

    Frame.io doesn't document the child ordering, so sort by whatever timestamp
    the child exposes and fall back to the last item if none is present.
    ponytail: timestamp heuristic — the chosen version is logged by the caller,
    so if it ever picks wrong we can pin the real field/order from prod logs.
    """
    if not children:
        return None

    def ts(c: dict) -> str:
        return c.get('inserted_at') or c.get('created_at') or c.get('updated_at') or ''

    if any(ts(c) for c in children):
        return max(children, key=ts)
    return children[-1]


def _resolve_stack_newest(stack_id: str) -> tuple[str, dict]:
    """Resolve (file_id, file_data) for the newest version in a version stack."""
    try:
        children = get_version_stack_children(ACCOUNT_ID, stack_id)
    except Exception as e:
        logger.exception(f"Failed to fetch version stack {stack_id}: {e}")
        return '', {}

    newest = _newest_child(children)
    if not newest or not newest.get('id'):
        logger.warning(f"Version stack {stack_id} has no resolvable versions; skipping")
        return '', {}

    logger.info(
        f"Version stack {stack_id}: recording newest version {newest['id']} "
        f"({newest.get('name')!r}) of {len(children)} version(s)"
    )
    try:
        return newest['id'], get_file(ACCOUNT_ID, newest['id'])
    except Exception as e:
        logger.exception(f"Failed to fetch newest version {newest['id']}: {e}")
        return '', {}


def _resolve_target_file(event: dict) -> tuple[str, dict]:
    """Resolve the (file_id, file_data) to enrich for this event.

    Most events point at a file. A `file.versioned` event points at the version
    stack instead, and GET /files/{stack_id} returns 422 — so on a 422 we retry
    the id as a version stack and record its newest version. Returns ('', {})
    when nothing usable resolves.
    """
    file_id = _extract_file_id(event)
    if not file_id:
        logger.warning(f"No file_id in event {event.get('id')}, skipping enrichment")
        return '', {}

    try:
        return file_id, get_file(ACCOUNT_ID, file_id)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 422:
            logger.info(f"{file_id} is not a file (422) — resolving as a version stack")
            return _resolve_stack_newest(file_id)
        logger.exception(f"Failed to fetch file {file_id}: {e}")
        return '', {}
    except Exception as e:
        logger.exception(f"Failed to fetch file {file_id}: {e}")
        return '', {}


def handle_event(event: dict):
    """Main entry point. Return True if something was written to an enabled backend."""
    event_type = event.get('type', '')

    if event_type not in ENRICHMENT_EVENTS:
        logger.info(f"Skipping enrichment for event type: {event_type}")
        return False

    file_id, file_data = _resolve_target_file(event)
    if not file_id:
        return False

    # The project name selects which Airtable table to write to.
    project_name = _resolve_project_name(event, file_data)
    if not project_name:
        logger.warning(f"No project name for file {file_id}; cannot route to a table — skipping")
        return False

    metadata = parse_metadata(file_data)

    # Filename goes into the Name (Production ID) column
    filename = file_data.get('name', '')

    updates = {
        'frameio_file_id': file_id,
        'production_id': filename,
    }

    # Map Frame.io metadata fields to Airtable columns.
    # Match field names case-insensitively so "Module"/"MODULE"/"module" all work.
    metadata_ci = {name.lower(): val for name, val in metadata.items()}
    for fio_field_name, key in METADATA_FIELD_MAP.items():
        value = metadata_ci.get(fio_field_name.lower())
        if value is None:
            continue
        if isinstance(value, list):
            continue
        updates[key] = value

    if not SHEETS_ENABLED and not AIRTABLE_ENABLED:
        logger.warning("No write backend enabled (SHEETS_ENABLED/AIRTABLE_ENABLED both off)")
        return False

    # Version-stack (R1 -> R2): if this asset belongs to a version stack, a prior
    # version's File ID may already be a sheet row. Collect the sibling File IDs
    # so the writer updates that row in place (new File ID + status) instead of
    # inserting a duplicate. Empty for the common non-versioned case.
    sibling_file_ids: list = []
    try:
        version_stack_id = resolve_version_stack_id(event, file_data)
        if version_stack_id:
            children = get_version_stack_children(ACCOUNT_ID, version_stack_id)
            sibling_file_ids = [
                c.get('id') for c in children
                if c.get('id') and c.get('id') != file_id
            ]
            logger.info(
                f"File {file_id} is in version stack {version_stack_id} with "
                f"{len(sibling_file_ids)} prior version(s)"
            )
    except Exception as e:
        logger.warning(f"Version-stack resolution failed for file {file_id}: {e}")

    # Terminal status: the asset has left this project's tracking, so its row is
    # deleted rather than updated (e.g. moved to "Full Length Lecture").
    status_value = updates.get('status', '')
    is_removal = bool(status_value) and _normalize_status(status_value) in _REMOVAL_STATUS_SET

    wrote = False

    if SHEETS_ENABLED:
        try:
            from sheets_writer import upsert_record as sheets_upsert
            result = None
            if is_removal:
                from sheets_writer import delete_record as sheets_delete
                result = sheets_delete(
                    file_id,
                    table_hint=project_name,
                    also_match_file_ids=sibling_file_ids,
                    allowed_prior_statuses=DELETABLE_PRIOR_STATUSES,
                )
                logger.info(
                    f"Sheets delete result: {result} for file {file_id} "
                    f"(project {project_name!r}, status {status_value!r})"
                )
            # Deletion only applies to R1/R2 Edits → Full Length Lecture. For any
            # other case (not a removal status, or the delete was skipped because
            # the prior status wasn't eligible) still write the row so the new
            # status is captured.
            if not is_removal or result == "skipped":
                result = sheets_upsert(
                    updates, table_hint=project_name, also_match_file_ids=sibling_file_ids
                )
                logger.info(f"Sheets update result: {result} for file {file_id} (project {project_name!r})")
            wrote = True
        except Exception as e:
            logger.exception(f"Failed to update Sheets for file {file_id}: {e}")

    if AIRTABLE_ENABLED:
        try:
            from airtable_writer import upsert_record as airtable_upsert
            result = airtable_upsert(updates, table_hint=project_name)
            logger.info(f"Airtable update result: {result} for file {file_id} (project {project_name!r})")
            wrote = True
        except Exception as e:
            logger.exception(f"Failed to update Airtable for file {file_id}: {e}")

    return wrote
