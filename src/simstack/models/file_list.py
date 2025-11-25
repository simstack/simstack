import re
from typing import Union, List, Any, Optional

from odmantic import EmbeddedModel, Field, Model

from simstack.models import simstack_model
from simstack.models.files import FileStack


class FileListMixin:
    """
    Mixin class containing common functionality for file list operations.
    """

    def __len__(self):
        """
        Returns the number of file stacks in the file list.

        :return: Number of file stacks
        :rtype: int
        """
        return len(self.file_stacks)

    def append(self, file_stack: FileStack):
        """
        Appends a FileStack to the file list.

        :param file_stack: The FileStack to append.
        :type file_stack: FileStack
        """
        self.file_stacks.append(file_stack)

    def extend(self, file_stacks: Union[List[FileStack], "FileListMixin"]):
        """
        Extends the file list with multiple FileStacks.

        :param file_stacks: List of FileStacks or another FileListMixin to extend with
        :type file_stacks: Union[List[FileStack], FileListMixin]
        """
        if hasattr(file_stacks, "file_stacks"):
            # It's another FileListMixin object
            self.file_stacks.extend(file_stacks.file_stacks)
        else:
            # It's a list
            self.file_stacks.extend(file_stacks)

    def insert(self, index: int, file_stack: FileStack):
        """
        Inserts a FileStack at the specified index.

        :param index: Index where to insert the FileStack
        :type index: int
        :param file_stack: The FileStack to insert
        :type file_stack: FileStack
        """
        self.file_stacks.insert(index, file_stack)

    def remove(self, file_stack: FileStack):
        """
        Removes the first occurrence of the specified FileStack.

        :param file_stack: The FileStack to remove
        :type file_stack: FileStack
        :raises ValueError: If the FileStack is not found
        """
        self.file_stacks.remove(file_stack)

    def pop(self, index: int = -1) -> FileStack:
        """
        Removes and returns the FileStack at the specified index.

        :param index: Index of the FileStack to remove (default is last item)
        :type index: int
        :return: The removed FileStack
        :rtype: FileStack
        :raises IndexError: If the index is out of range
        """
        return self.file_stacks.pop(index)

    def clear(self):
        """
        Removes all FileStacks from the file list.
        """
        self.file_stacks.clear()

    def index(self, file_stack: FileStack, start: int = 0, stop: int = None) -> int:
        """
        Returns the index of the first occurrence of the specified FileStack.

        :param file_stack: The FileStack to find
        :type file_stack: FileStack
        :param start: Start index for search
        :type start: int
        :param stop: Stop index for search
        :type stop: int
        :return: Index of the FileStack
        :rtype: int
        :raises ValueError: If the FileStack is not found
        """
        if stop is None:
            return self.file_stacks.index(file_stack, start)
        return self.file_stacks.index(file_stack, start, stop)

    def count(self, file_stack: FileStack) -> int:
        """
        Returns the number of occurrences of the specified FileStack.

        :param file_stack: The FileStack to count
        :type file_stack: FileStack
        :return: Number of occurrences
        :rtype: int
        """
        return self.file_stacks.count(file_stack)

    def reverse(self):
        """
        Reverses the order of FileStacks in the file list.
        """
        self.file_stacks.reverse()

    def sort(self, key=None, reverse: bool = False):
        """
        Sorts the FileStacks in the file list.

        :param key: Function to extract comparison key from each FileStack
        :param reverse: If True, sort in descending order
        :type reverse: bool
        """
        self.file_stacks.sort(key=key, reverse=reverse)

    def copy(self) -> List[FileStack]:
        """
        Returns a shallow copy of the file stacks list.

        :return: Copy of the file stacks list
        :rtype: List[FileStack]
        """
        return self.file_stacks.copy()

    def __getitem__(
        self, index: Union[int, slice]
    ) -> Union[FileStack, List[FileStack]]:
        """
        Gets FileStack(s) at the specified index or slice.

        :param index: Index or slice
        :type index: Union[int, slice]
        :return: FileStack or list of FileStacks
        :rtype: Union[FileStack, List[FileStack]]
        """
        return self.file_stacks[index]

    def __setitem__(
        self, index: Union[int, slice], value: Union[FileStack, List[FileStack]]
    ):
        """
        Sets FileStack(s) at the specified index or slice.

        :param index: Index or slice
        :type index: Union[int, slice]
        :param value: FileStack or list of FileStacks to set
        :type value: Union[FileStack, List[FileStack]]
        """
        self.file_stacks[index] = value

    def __delitem__(self, index: Union[int, slice]):
        """
        Deletes FileStack(s) at the specified index or slice.

        :param index: Index or slice
        :type index: Union[int, slice]
        """
        del self.file_stacks[index]

    def __iter__(self):
        """
        Returns an iterator over the FileStacks.

        :return: Iterator over FileStacks
        """
        return iter(self.file_stacks)

    def __contains__(self, file_stack: FileStack) -> bool:
        """
        Checks if a FileStack is in the file list.

        :param file_stack: The FileStack to check for
        :type file_stack: FileStack
        :return: True if the FileStack is in the list, False otherwise
        :rtype: bool
        """
        return file_stack in self.file_stacks

    def __bool__(self) -> bool:
        """
        Returns True if the file list is not empty, False otherwise.

        :return: True if file list has items, False if empty
        :rtype: bool
        """
        return bool(self.file_stacks)

    def __repr__(self) -> str:
        """
        Returns a string representation of the file list.

        :return: String representation
        :rtype: str
        """
        return f"{self.__class__.__name__}(file_stacks={self.file_stacks!r})"

    def find(self, pattern: str) -> Union[FileStack, None]:
        """
        Searches through file stacks for the first file name matching the given regex pattern.

        :param pattern: Regular expression pattern to match against file names
        :type pattern: str
        :return: First matching FileStack or None if no match found
        :rtype: Union[FileStack, None]
        """
        for file_stack in self.file_stacks:
            if file_stack.name and re.search(pattern, file_stack.name):
                return file_stack
        return None

    def find_all(self, pattern: str) -> List[FileStack]:
        """
        Searches through file stacks for all files with names matching the given regex pattern.

        :param pattern: Regular expression pattern to match against file names
        :type pattern: str
        :return: List of all matching FileStacks
        :rtype: List[FileStack]
        """
        matches = []
        for file_stack in self.file_stacks:
            if file_stack.name and re.search(pattern, file_stack.name):
                matches.append(file_stack)
        return matches

    def filter_by_size(
        self, min_size: int = None, max_size: int = None
    ) -> List[FileStack]:
        """
        Filters FileStacks by size range.

        :param min_size: Minimum file size (inclusive)
        :type min_size: int
        :param max_size: Maximum file size (inclusive)
        :type max_size: int
        :return: List of FileStacks within the size range
        :rtype: List[FileStack]
        """
        results = []
        for file_stack in self.file_stacks:
            if min_size is not None and file_stack.size < min_size:
                continue
            if max_size is not None and file_stack.size > max_size:
                continue
            results.append(file_stack)
        return results

    def filter_by_property(self, property_name: str, value: Any) -> List[FileStack]:
        """
        Filters FileStacks by a specific property value.

        :param property_name: Name of the property to filter by
        :type property_name: str
        :param value: Value to match
        :type value: Any
        :return: List of matching FileStacks
        :rtype: List[FileStack]
        """
        results = []
        for file_stack in self.file_stacks:
            if (
                hasattr(file_stack, property_name)
                and getattr(file_stack, property_name) == value
            ):
                results.append(file_stack)
        return results

    def sort_by_name(self, reverse: bool = False):
        """
        Sorts FileStacks by name.

        :param reverse: If True, sort in descending order
        :type reverse: bool
        """
        self.file_stacks.sort(key=lambda fs: fs.name or "", reverse=reverse)

    def sort_by_size(self, reverse: bool = False):
        """
        Sorts FileStacks by size.

        :param reverse: If True, sort in descending order
        :type reverse: bool
        """
        self.file_stacks.sort(key=lambda fs: fs.size, reverse=reverse)


@simstack_model
class FileList(EmbeddedModel, FileListMixin):
    file_stacks: List[FileStack] = Field(
        default_factory=list, description="List of file stacks"
    )


@simstack_model
class FileListModel(Model, FileListMixin):
    file_stacks: List[FileStack] = Field(
        default_factory=list, description="List of file stacks"
    )


@simstack_model
class FileListIO(Model):
    file_list: FileList = Field(default_factory=FileList)
    task_status: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None
