import inspect
import re
from typing import Optional, Dict, Any, List


class DocstringParser:
    """
    Parse a docstring into structured components.

    All component getters return None when the corresponding section is not present.
    Supported sections:
      - Args: / Parameters:
      - Returns:
      - SimstackResult:
      - CalledNodes:
      - Raises:
    """

    _SECTION_NAMES = ("Args:", "Parameters:", "Returns:", "SimstackResult:", "CalledNodes:", "Raises:")

    def __init__(self, docstring: Optional[str]) -> None:
        self._raw = docstring or ""
        self._doc = inspect.cleandoc(self._raw) if self._raw else ""
        self._cache: Dict[str, Any] = {}

    def _section(self, name: str, until: tuple[str, ...]) -> Optional[str]:
        """
        Extract the body text after `name` up to any of the `until` markers (or end).
        Returns None if the section marker isn't present.
        """
        if not self._doc:
            return None

        # e.g. r"Returns:(.*?)(?:SimstackResult:|CalledNodes:|Raises:|$)"
        until_alt = "|".join(map(re.escape, until)) + "|$"
        pattern = rf"{re.escape(name)}(.*?)(?:{until_alt})"
        match = re.search(pattern, self._doc, re.DOTALL)
        if not match:
            return None

        body = match.group(1).strip()
        return body if body else None

    def description(self) -> Optional[str]:
        if "description" in self._cache:
            return self._cache["description"]

        if not self._doc:
            self._cache["description"] = None
            return None

        match = re.search(
            r"^(.*?)(?:Args:|Parameters:|Returns:|SimstackResult:|CalledNodes:|Raises:|$)",
            self._doc,
            re.DOTALL,
        )
        desc = match.group(1).strip() if match else ""
        self._cache["description"] = desc if desc else None
        return self._cache["description"]

    def params(self) -> Optional[Dict[str, Dict[str, Optional[str]]]]:
        if "params" in self._cache:
            return self._cache["params"]

        param_text = self._section("Args:", ("Returns:", "SimstackResult:", "CalledNodes:", "Raises:"))
        if param_text is None:
            param_text = self._section("Parameters:", ("Returns:", "SimstackResult:", "CalledNodes:", "Raises:"))

        if not param_text:
            self._cache["params"] = None
            return None

        params: Dict[str, Dict[str, Optional[str]]] = {}
        param_matches = re.finditer(
            r"(\w+)\s*(?:\(([^)]+)\))?\s*:\s*(.+?)(?=\n\s*\w+\s*:|$)",
            param_text,
            re.DOTALL,
        )
        for match in param_matches:
            param_name = match.group(1)
            param_type = match.group(2)  # Maybe None
            param_desc = match.group(3).strip()
            params[param_name] = {"type": param_type, "description": param_desc}

        self._cache["params"] = params if params else None
        return self._cache["params"]

    def returns(self) -> Optional[Dict[str, str]]:
        if "returns" in self._cache:
            return self._cache["returns"]

        return_text = self._section("Returns:", ("SimstackResult:", "CalledNodes:", "Raises:"))
        if not return_text:
            self._cache["returns"] = None
            return None

        self._cache["returns"] = {"description": return_text}
        return self._cache["returns"]

    def simstack_results(self) -> Optional[Dict[str, Dict[str, str]]]:
        if "simstack_results" in self._cache:
            return self._cache["simstack_results"]

        simstack_text = self._section("SimstackResult:", ("CalledNodes:", "Raises:"))
        if not simstack_text:
            self._cache["simstack_results"] = None
            return None

        simstack_results: Dict[str, Dict[str, str]] = {}
        simstack_matches = re.finditer(
            r"(\w+)\s*\(([^)]+)\)\s*(.+?)(?=\n\s*\w+\s*\(|$)",
            simstack_text,
            re.DOTALL,
        )
        for match in simstack_matches:
            result_name = match.group(1)
            result_type = match.group(2).strip()
            result_desc = match.group(3).strip()
            simstack_results[result_name] = {"name": result_name, "type": result_type, "description": result_desc}

        self._cache["simstack_results"] = simstack_results if simstack_results else None
        return self._cache["simstack_results"]

    def called_nodes(self) -> Optional[List[str]]:
        if "called_nodes" in self._cache:
            return self._cache["called_nodes"]

        called_nodes_text = self._section("CalledNodes:", ("Raises:",))
        if not called_nodes_text:
            self._cache["called_nodes"] = None
            return None

        nodes = [
            line.strip().lstrip("-").strip()
            for line in called_nodes_text.split("\n")
            if line.strip()
        ]
        self._cache["called_nodes"] = nodes if nodes else None
        return self._cache["called_nodes"]

    def raises(self) -> Optional[Dict[str, Dict[str, str]]]:
        if "raises" in self._cache:
            return self._cache["raises"]

        raises_text = self._section("Raises:", ())
        if not raises_text:
            self._cache["raises"] = None
            return None

        raises: Dict[str, Dict[str, str]] = {}
        raises_matches = re.finditer(
            r"(\w+)\s*:\s*(.+?)(?=\n\s*\w+\s*:|$)",
            raises_text,
            re.DOTALL,
        )
        for match in raises_matches:
            exception_name = match.group(1)
            exception_desc = match.group(2).strip()
            raises[exception_name] = {"description": exception_desc}

        self._cache["raises"] = raises if raises else None
        return self._cache["raises"]

    def as_dict(self) -> Dict[str, Any]:
        """
        Backward-compatible structure (always includes keys),
        while still using the None-returning component getters internally.
        """
        return {
            "description": self.description() or "",
            "params": self.params() or {},
            "returns": self.returns() or {},
            "simstack_results": self.simstack_results() or {},
            "called_nodes": self.called_nodes() or [],
            "raises": self.raises() or {},
        }


def parse_docstring(docstring: Optional[str]) -> Dict[str, Any]:
    """
    Backward-compatible wrapper around DocstringParser.

    Prefer using DocstringParser directly when you want per-section access with
    None when a section is missing.
    """
    return DocstringParser(docstring).as_dict()
