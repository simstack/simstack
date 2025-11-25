import unittest
from simstack.util.db_logger import extract_resource_id


class TestResourcePatternExtraction(unittest.TestCase):
    """Test cases for extracting resource IDs from log messages."""

    def test_basic_resource_id_extraction(self):
        """Test extracting simple alphanumeric resource IDs."""
        # Test basic resource ID extraction
        message = "Processing resource with resource_id: abc123"
        self.assertEqual(extract_resource_id(message), "abc123")

    def test_resource_id_with_underscore(self):
        """Test extracting resource IDs containing underscores."""
        message = "Downloaded resource_id: file_resource_123"
        self.assertEqual(extract_resource_id(message), "file_resource_123")

    def test_resource_id_with_hyphen(self):
        """Test extracting resource IDs containing hyphens."""
        message = "Analyzing resource_id: data-set-2023"
        self.assertEqual(extract_resource_id(message), "data-set-2023")

    def test_resource_id_with_mixed_characters(self):
        """Test extracting resource IDs with mixed special characters."""
        message = "Working with resource_id: complex-id_123"
        self.assertEqual(extract_resource_id(message), "complex-id_123")

    def test_resource_id_at_start_of_message(self):
        """Test extracting resource IDs at the beginning of messages."""
        message = "resource_id: front-loaded followed by text"
        self.assertEqual(extract_resource_id(message), "front-loaded")

    def test_resource_id_at_end_of_message(self):
        """Test extracting resource IDs at the end of messages."""
        message = "End of log entry with resource_id: end-123"
        self.assertEqual(extract_resource_id(message), "end-123")

    def test_resource_id_with_surrounding_content(self):
        """Test extracting resource IDs with content before and after."""
        message = "Starting process on resource_id: mid-point-45 with parameters"
        self.assertEqual(extract_resource_id(message), "mid-point-45")

    def test_multiple_resource_ids_returns_first(self):
        """Test that only the first resource ID is extracted when multiple exist."""
        message = "resource_id: first-id and then resource_id: second-id"
        self.assertEqual(extract_resource_id(message), "first-id")

    def test_resource_id_with_dots(self):
        """Test extracting resource IDs containing periods."""
        message = "Using resource_id: version.1.2.3"
        self.assertEqual(extract_resource_id(message), "version.1.2.3")

    def test_resource_id_missing(self):
        """Test handling messages with no resource ID."""
        message = "This message has no resource id pattern"
        self.assertIsNone(extract_resource_id(message))

    def test_resource_id_empty(self):
        """Test handling empty resource ID."""
        message = "Invalid resource_id: "
        self.assertIsNone(extract_resource_id(message))

    def test_resource_id_mongodb_objectid(self):
        """Test extracting MongoDB ObjectIDs as resource IDs."""
        object_id = "507f1f77bcf86cd799439011"  # Example MongoDB ObjectId
        message = f"Fetched resource_id: {object_id}"
        self.assertEqual(extract_resource_id(message), object_id)

    def test_resource_id_uuid_format(self):
        """Test extracting UUIDs as resource IDs."""
        uuid = "123e4567-e89b-12d3-a456-426614174000"  # Example UUID
        message = f"Created resource_id: {uuid}"
        self.assertEqual(extract_resource_id(message), uuid)


if __name__ == "__main__":
    unittest.main()
