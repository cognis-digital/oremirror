"""oremirror — OCI registry mirror/sync for disconnected environments.

Part of the Cognis Neural Suite. Plan, pull to an OCI image-layout, verify
integrity, and push across the air-gap. Standard library only.
"""

from oremirror.core import (
    TOOL_NAME,
    TOOL_VERSION,
    OreError,
    RegistryError,
    ImageRef,
    BlobPlan,
    ImagePlan,
    TransferPlan,
    VerifyResult,
    RegistryClient,
    parse_ref,
    load_image_list,
    parse_image_list,
    build_plan,
    pull_to_layout,
    verify_layout,
    push_layout,
    digest_bytes,
    digest_file,
)

__version__ = TOOL_VERSION

__all__ = [
    "TOOL_NAME",
    "TOOL_VERSION",
    "__version__",
    "OreError",
    "RegistryError",
    "ImageRef",
    "BlobPlan",
    "ImagePlan",
    "TransferPlan",
    "VerifyResult",
    "RegistryClient",
    "parse_ref",
    "load_image_list",
    "parse_image_list",
    "build_plan",
    "pull_to_layout",
    "verify_layout",
    "push_layout",
    "digest_bytes",
    "digest_file",
]
