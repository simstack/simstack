"""Disabled archive prototype retained for a future, separately reviewed change.

The public entrypoints intentionally fail closed. Permanent storage ownership and
safe source cleanup must be designed and covered by production-path tests before
this module can be enabled.
"""

from datetime import datetime
from typing import List, Optional

from simstack.models.document import Model, Field

from simstack.core.node import node
from simstack.models import FileStack, FileList, simstack_model


ARCHIVE_DISABLED_MESSAGE = (
    "File archiving is disabled for this migration until permanent-resource "
    "semantics and non-destructive cleanup are implemented and tested."
)


class ArchiveFeatureDisabledError(RuntimeError):
    """Raised when code tries to use the excluded archive prototype."""


def archive_one_file(file_stack: FileStack,**kwargs):
    raise ArchiveFeatureDisabledError(ARCHIVE_DISABLED_MESSAGE)

@node
async def archive_file(file_stack: FileStack,**kwargs):
    raise ArchiveFeatureDisabledError(ARCHIVE_DISABLED_MESSAGE)

@node
def archive_files(file_list: FileList,**kwargs):
    raise ArchiveFeatureDisabledError(ARCHIVE_DISABLED_MESSAGE)


@simstack_model
class ArchiveConfig(Model):
    call_paths: List[str] = Field(default_factory=list)
    archive_resource: str = Field(default="archive_resource", description="Name of the resource where the files will be archived")
    in_memory: bool = Field(default=False, description="Whether to archive files in memory")
    use_time_window: bool = Field(default=False, description="Whether to archive files within a time window")
    start_date: Optional[datetime] = Field(default=None, description="Start of the period to archive files for")
    end_date: Optional[datetime] = Field(default=None, description="End end of the period to archive files for")
    include_patterns: List[str] = Field(default_factory=list, description="List of file patterns to include")
    exclude_patterns: List[str] = Field(default_factory=list, description="List of file patterns to exclude")
    min_size: int = Field(default=0, description="Minimum size of files to archive")
    filter_by_resource: bool = Field(default=False, description="Whether to only archive files that have a location on the current resource")


@node
async def archive_node(archive_config: ArchiveConfig, **kwargs):
    """
    Archives FileStacks matching the criteria in archive_config and deletes local instances upon success.

    Args:
        archive_config (ArchiveConfig): Configuration for the archive process.
        **kwargs: Additional arguments.
    """
    raise ArchiveFeatureDisabledError(ARCHIVE_DISABLED_MESSAGE)
