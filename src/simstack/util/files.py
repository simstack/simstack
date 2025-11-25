import os
from datetime import datetime
from typing import Optional, Dict, Any, List

from odmantic import Field
from pydantic import BaseModel


class SshAuthMethod(BaseModel):
    """ """

    # Authentication type
    auth_type: str = Field(
        description="Authentication type (password, key, agent, none)"
    )

    # Password authentication
    password: Optional[str] = Field(
        default=None,
        description="Password for authentication (should be stored securely)",
    )

    # Key-based authentication
    key_filename: Optional[str] = Field(
        default=None, description="Path to the private key file"
    )
    key_passphrase: Optional[str] = Field(
        default=None, description="Passphrase for the private key"
    )

    # Allow host key to be added automatically to known_hosts
    allow_host_key_add: bool = Field(
        default=True, description="Whether to automatically add host key to known_hosts"
    )

    # Host key verification options
    host_key_policy: str = Field(
        default="ask", description="Host key policy (ask, auto_add, strict)"
    )
    known_hosts_file: Optional[str] = Field(
        default=None, description="Path to known_hosts file"
    )

    model_config = {"extra": "allow"}


class FfspecFile(BaseModel):
    """
    Representation of a file with protocol-specific details, metadata, and utility methods
    for remote or local operations.

    This class facilitates working with files across different storage protocols such as
    local filesystem, SFTP, SCP, or cloud-based protocols. It provides attributes for
    authenticating, accessing, and manipulating files as well as methods for operations
    like copying files, creating URIs, and testing connections.

    :ivar path: Full path to the file.
    :type path: str
    :ivar name: Name of the file.
    :type name: str
    :ivar extension: File extension.
    :type extension: str
    :ivar protocol: Storage protocol (e.g., 'file', 's3', 'http', 'sftp', 'scp').
    :type protocol: str
    :ivar host: Host name or address where the file is located.
    :type host: Optional[str]
    :ivar port: Port number if applicable.
    :type port: Optional[int]
    :ivar username: Username for authentication if required.
    :type username: Optional[str]
    :ivar ssh_auth: SSH authentication configuration.
    :type ssh_auth: Optional[SshAuthMethod]
    :ivar size: Size of the file in bytes.
    :type size: int
    :ivar created_at: Creation timestamp.
    :type created_at: datetime
    :ivar modified_at: Last modification timestamp.
    :type modified_at: datetime
    :ivar storage_options: Protocol-specific storage options.
    :type storage_options: Dict[str, Any]
    :ivar checksum: File content checksum.
    :type checksum: Optional[str]
    :ivar content_type: MIME type of the file.
    :type content_type: Optional[str]
    :ivar parent_path: Path to parent directory.
    :type parent_path: Optional[str]
    :ivar is_directory: Whether this is a directory.
    :type is_directory: bool
    :ivar children: Child file/directory paths if this is a directory.
    :type children: List[str]
    :ivar last_accessed: Last access timestamp.
    :type last_accessed: Optional[datetime]
    :ivar access_count: Number of times this file has been accessed.
    :type access_count: int
    :ivar metadata: Additional metadata as key-value pairs.
    :type metadata: Dict[str, Any]
    """

    # Basic file information
    path: str = Field(description="Full path to the file")
    name: str = Field(description="Name of the file")
    extension: str = Field(description="File extension")

    # Protocol and location information
    protocol: str = Field(
        description="Storage protocol (e.g., 'file', 's3', 'http', 'sftp', 'scp')"
    )
    host: Optional[str] = Field(
        default=None, description="Host name or address where the file is located"
    )
    port: Optional[int] = Field(default=None, description="Port number if applicable")

    # Authentication information
    username: Optional[str] = Field(
        default=None, description="Username for authentication if required"
    )
    ssh_auth: Optional[SshAuthMethod] = Field(
        default=None, description="SSH authentication configuration"
    )

    # File metadata
    size: int = Field(description="Size of the file in bytes")
    created_at: datetime = Field(description="Creation timestamp")
    modified_at: datetime = Field(description="Last modification timestamp")

    # Ffspec-specific attributes
    storage_options: Dict[str, Any] = Field(
        default_factory=dict, description="Protocol-specific storage options"
    )

    # Content addressing
    checksum: Optional[str] = Field(default=None, description="File content checksum")
    content_type: Optional[str] = Field(
        default=None, description="MIME type of the file"
    )

    # Hierarchical structure support
    parent_path: Optional[str] = Field(
        default=None, description="Path to parent directory"
    )
    is_directory: bool = Field(default=False, description="Whether this is a directory")
    children: List[str] = Field(
        default_factory=list,
        description="Child file/directory paths if this is a directory",
    )

    # Access metadata
    last_accessed: Optional[datetime] = Field(
        default=None, description="Last access timestamp"
    )
    access_count: int = Field(
        default=0, description="Number of times this file has been accessed"
    )

    # Custom metadata
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata as key-value pairs"
    )

    @classmethod
    def from_ssh_key(
        cls,
        host: str,
        path: str,
        username: str,
        key_filename: str,
        protocol: str = "sftp",
        port: int = 22,
        key_passphrase: Optional[str] = None,
        host_key_policy: str = "auto_add",
        **kwargs,
    ) -> "FfspecFile":
        """
        Create a FfspecFile instance with SSH key authentication.

        Args:
            host: Remote host name or IP address
            path: Path to the file on the remote host
            username: SSH username for authentication
            key_filename: Path to the SSH private key file
            protocol: Protocol to use, either 'sftp' or 'scp' (default: 'sftp')
            port: SSH port on the remote host (default: 22)
            key_passphrase: Passphrase for the SSH key if it's encrypted (default: None)
            host_key_policy: Host key policy - 'auto_add', 'strict', or 'ask' (default: 'auto_add')
            **kwargs: Additional attributes to set on the model

        Returns:
            FfspecFile: A new instance representing the remote file
        """
        from pathlib import Path

        # Validate protocol
        if protocol not in ["sftp", "scp"]:
            raise ValueError(f"Protocol must be 'sftp' or 'scp', got '{protocol}'")

        # Validate that the key file exists
        if not os.path.exists(key_filename):
            raise FileNotFoundError(f"SSH key file not found: {key_filename}")

        # Ensure path is absolute (starts with /)
        if not path.startswith("/"):
            path = "/" + path

        # Create SSH authentication config with proper host key policy
        ssh_auth = SshAuthMethod(
            auth_type="key",
            key_filename=key_filename,
            key_passphrase=key_passphrase,
            host_key_policy=host_key_policy,
            allow_host_key_add=(host_key_policy == "auto_add"),
        )

        # Extract file name and extension from path
        p = Path(path)
        name = p.name
        extension = p.suffix.lstrip(".") if p.suffix else ""
        parent_path = str(p.parent) if p.parent != p else None

        # Create comprehensive storage options for fsspec
        # Note: Removed missing_host_key_policy as it's not a valid paramiko connection parameter
        storage_options = {
            "host": host,
            "port": port,
            "username": username,
            "key_filename": key_filename,
            # Connection timeouts
            "timeout": 30,
            "banner_timeout": 30,
            "auth_timeout": 30,
        }

        # Add key passphrase if provided
        if key_passphrase:
            storage_options["password"] = key_passphrase

        # Create the instance
        return cls(
            path=path,
            name=name,
            extension=extension,
            protocol=protocol,
            host=host,
            port=port,
            username=username,
            ssh_auth=ssh_auth,
            storage_options=storage_options,
            parent_path=parent_path,
            # Use values from kwargs or sensible defaults for required fields
            size=kwargs.pop("size", 0),
            created_at=kwargs.pop("created_at", datetime.now()),
            modified_at=kwargs.pop("modified_at", datetime.now()),
            is_directory=kwargs.pop("is_directory", False),
            **kwargs,
        )

    def get_filesystem(self):
        """
        Create and return a fsspec filesystem object for this file.

        Returns:
            object: An fsspec filesystem instance
        """
        import importlib

        try:
            # Import the appropriate filesystem module
            fsspec = importlib.import_module("fsspec")

            # Handle SSH-specific protocols
            if self.protocol in ["sftp", "scp"]:
                # Create a copy of storage options to avoid modifying the original
                storage_options = dict(self.storage_options)

                # Add SSH auth options if available
                if self.ssh_auth:
                    if self.ssh_auth.auth_type == "key":
                        storage_options["key_filename"] = self.ssh_auth.key_filename
                        if self.ssh_auth.key_passphrase:
                            storage_options["password"] = self.ssh_auth.key_passphrase
                    elif self.ssh_auth.auth_type == "password":
                        storage_options["password"] = self.ssh_auth.password

                # Create the filesystem without the problematic missing_host_key_policy parameter
                return fsspec.filesystem(self.protocol, **storage_options)
            else:
                # Handle other protocols
                return fsspec.filesystem(self.protocol, **self.storage_options)

        except (ImportError, KeyError) as e:
            raise ValueError(
                f"Could not create filesystem for protocol '{self.protocol}': {e}"
            )

    def can_connect(self) -> bool:
        """
        Test if we can connect to the remote host using the provided credentials.

        Returns:
            bool: True if connection is successful, False otherwise
        """
        if self.protocol not in ["sftp", "scp"]:
            # Only applicable for SSH-based protocols
            return False

        try:
            fs = self.get_filesystem()
            # Try to list the parent directory
            fs.ls(os.path.dirname(self.path))
            return True
        except Exception:
            return False

    def to_uri(self) -> str:
        """
        Convert this model to a URI/URL representation.

        Note: SSH private key information is not included in the URI.

        Returns:
            str: URI string representing this file
        """
        import urllib.parse

        # Build the netloc part
        netloc = ""
        if self.username:
            netloc = f"{self.username}@"
        if self.host:
            netloc += self.host
        if self.port and self.port != 22:  # Only include non-standard SSH port
            netloc += f":{self.port}"

        # Assemble the URI
        return urllib.parse.urlunparse(
            (
                self.protocol,  # scheme
                netloc,  # netloc
                self.path,  # path
                "",  # params
                "",  # query (not including auth info for security)
                "",  # fragment
            )
        )

    def copy_to_local(self, local_path: Optional[str] = None) -> str:
        """
        Copy the remote file to a local path.

        Args:
            local_path: Local path to copy the file to. If None, a temporary file is created.

        Returns:
            str: Path to the local copy of the file
        """
        import tempfile

        if self.is_directory:
            raise ValueError(
                "Cannot copy a directory, only individual files are supported"
            )

        # Create a destination path if none is provided
        if local_path is None:
            temp_dir = tempfile.gettempdir()
            local_path = os.path.join(temp_dir, self.name)

        # Get the filesystem and copy the file
        fs = self.get_filesystem()
        fs.get(self.path, local_path)

        return local_path

    def copy_from_local(self, local_path: str) -> None:
        """
        Copy a local file to the remote location represented by this model.

        Args:
            local_path: Path to the local file

        Returns:
            None
        """
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"Local file {local_path} does not exist")

        # Get the filesystem and copy the file
        fs = self.get_filesystem()
        fs.put(local_path, self.path)

        # Update file size and modification time
        try:
            info = fs.info(self.path)
            self.size = info.get("size", 0)
            self.modified_at = datetime.now()
        except Exception:
            pass
