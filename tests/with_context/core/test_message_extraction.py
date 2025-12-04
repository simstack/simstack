from simstack.util.db_logger import extract_task_id


def test_task_id_extraction():
    # Test case 1: Basic extraction
    message = "Processing data for task_id: ABC123"
    assert extract_task_id(message) == "ABC123", "Failed to extract simple task ID"

    # Test case 2: Task ID with special characters
    message = "Error occurred in task_id: 456-XYZ-789 during execution"
    assert (
        extract_task_id(message) == "456-XYZ-789"
    ), "Failed to extract task ID with hyphens"

    # Test case 3: Task ID with no trailing text
    message = "task_id: T-1000 is currently running"
    assert extract_task_id(message) == "T-1000", "Failed to extract task ID with prefix"

    # Test case 4: No task ID in message
    message = "No task id in this message"
    assert (
        extract_task_id(message) is None
    ), "Should return None when no task ID is present"

    # Test case 5: Different format (should not match)
    message = "Different format taskid: ABC"
    assert extract_task_id(message) is None, "Should not match different format"

    # Test case 6: No space after colon
    message = "task_id:QR456 (no space after colon)"
    assert (
        extract_task_id(message) is None
    ), "Failed to extract task ID without space after colon"

    # Test case 7: Task ID with complex structure
    message = "Message with task_id: ABC-123-DEF-456 and more text"
    assert (
        extract_task_id(message) == "ABC-123-DEF-456"
    ), "Failed to extract complex task ID"

    # Test case 8: Task ID at the end of message
    message = "End of log task_id: END123"
    assert (
        extract_task_id(message) == "END123"
    ), "Failed to extract task ID at end of message"

    # Test case 9: Multiple task IDs (should return first match)
    message = "task_id: FIRST123 and then task_id: SECOND456"
    assert (
        extract_task_id(message) == "FIRST123"
    ), "Should return first task ID when multiple are present"

    # Test case 10: Task ID with underscores and dots
    message = "Processing task_id: user_123.456.789"
    assert (
        extract_task_id(message) == "user_123.456.789"
    ), "Failed to extract task ID with underscores and dots"
