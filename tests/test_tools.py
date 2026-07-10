import pytest
from typing import Annotated, Literal, Optional, Union

from lipas.tools import SideEffectClass, Tool, ValidationError, tool


# ---------------------------------------------------------------------------
# Example 1: simplest
# ---------------------------------------------------------------------------

def test_simplest_tool():
    @tool(side_effect=SideEffectClass.PURE)
    def add(x: int, y: int) -> int:
        """Add two numbers."""
        return x + y

    assert isinstance(add, Tool)
    assert add.name == "add"
    assert add.description == "Add two numbers."
    assert add.parameters_schema == {
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
        },
        "required": ["x", "y"],
    }
    assert add.invoke(x=1, y=2) == 3


def test_tool_not_directly_callable():
    @tool(side_effect=SideEffectClass.PURE)
    def add(x: int) -> int:
        """Add."""
        return x

    with pytest.raises(TypeError, match="not directly callable"):
        add(1)


# ---------------------------------------------------------------------------
# Example 2: Annotated description
# ---------------------------------------------------------------------------

def test_annotated_description_and_defaults():
    @tool(side_effect=SideEffectClass.PURE)
    def search(
        query: Annotated[str, "The search query"],
        limit: Annotated[int, "Max results"] = 10,
    ) -> list[str]:
        """Search the knowledge base."""
        return []

    props = search.parameters_schema["properties"]
    assert props["query"] == {"type": "string", "description": "The search query"}
    assert props["limit"] == {"type": "integer", "description": "Max results"}
    assert search.parameters_schema["required"] == ["query"]


def test_annotated_multiple_metadata_first_string_wins():
    class Ge:  # validator-style metadata (ignored)
        def __init__(self, v): self.v = v

    @tool(side_effect=SideEffectClass.PURE)
    def f(x: Annotated[int, Ge(0), "the x value", Ge(10), "ignored second"]) -> None:
        """F."""

    assert f.parameters_schema["properties"]["x"] == {
        "type": "integer",
        "description": "the x value",
    }


# ---------------------------------------------------------------------------
# Example 3: Optional (anyOf form)
# ---------------------------------------------------------------------------

def test_optional_emits_anyof():
    @tool(side_effect=SideEffectClass.PURE)
    def fetch(url: str, timeout: Optional[float] = None) -> str:
        """Fetch."""
        return ""

    assert fetch.parameters_schema["properties"]["timeout"] == {
        "anyOf": [{"type": "number"}, {"type": "null"}]
    }
    assert fetch.parameters_schema["required"] == ["url"]


def test_pep604_union_syntax():
    @tool(side_effect=SideEffectClass.PURE)
    def fetch(url: str, timeout: float | None = None) -> str:
        """Fetch."""
        return ""

    assert fetch.parameters_schema["properties"]["timeout"] == {
        "anyOf": [{"type": "number"}, {"type": "null"}]
    }


# ---------------------------------------------------------------------------
# Example 4: Literal + composition
# ---------------------------------------------------------------------------

def test_literal():
    @tool(side_effect=SideEffectClass.PURE)
    def set_status(
        issue_id: int,
        status: Annotated[Literal["open", "in_progress", "closed"], "New status"],
    ) -> None:
        """Update an issue's status."""

    assert set_status.parameters_schema["properties"]["status"] == {
        "enum": ["open", "in_progress", "closed"],
        "description": "New status",
    }


def test_optional_literal_composes_via_anyof():
    @tool(side_effect=SideEffectClass.PURE)
    def f(x: Optional[Literal["a", "b"]] = None) -> None:
        """F."""

    assert f.parameters_schema["properties"]["x"] == {
        "anyOf": [{"enum": ["a", "b"]}, {"type": "null"}]
    }


def test_list_of_optional_composes_via_anyof():
    @tool(side_effect=SideEffectClass.PURE)
    def f(xs: list[Optional[str]]) -> None:
        """F."""

    assert f.parameters_schema["properties"]["xs"] == {
        "type": "array",
        "items": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    }


# ---------------------------------------------------------------------------
# Example 5: schema= escape hatch
# ---------------------------------------------------------------------------

def test_schema_escape_hatch():
    custom = {
        "type": "object",
        "properties": {"filter": {"type": "object"}},
        "required": ["filter"],
    }

    @tool(side_effect=SideEffectClass.PURE, name="run_query", description="Run a query.", schema=custom)
    def query(filter: dict) -> list[dict]:
        return []

    assert query.name == "run_query"
    assert query.description == "Run a query."
    assert query.parameters_schema is custom


def test_schema_skips_annotated_inference():
    """schema= is a full off switch: Annotated hints are NOT consulted."""
    @tool(side_effect=SideEffectClass.PURE,
        description="Custom.",
        schema={"type": "object", "properties": {}, "required": []},
    )
    def f(x: Annotated[str, "would-be-description"]) -> None:
        pass

    assert f.parameters_schema == {
        "type": "object", "properties": {}, "required": []
    }


# ---------------------------------------------------------------------------
# Error paths — the two most likely user first-touches
# ---------------------------------------------------------------------------

def test_no_docstring_raises():
    with pytest.raises(ValidationError, match="no description"):
        @tool(side_effect=SideEffectClass.PURE)
        def f(x: int) -> int:
            return x


def test_no_type_annotation_raises():
    with pytest.raises(ValidationError, match="no type annotation"):
        @tool(side_effect=SideEffectClass.PURE)
        def f(x) -> int:
            """F."""
            return x


def test_var_positional_rejected_with_targeted_message():
    with pytest.raises(ValidationError, match=r"\*args/\*\*kwargs"):
        @tool(side_effect=SideEffectClass.PURE)
        def f(*args: int) -> int:
            """F."""
            return 0


def test_var_keyword_rejected_with_targeted_message():
    with pytest.raises(ValidationError, match=r"\*args/\*\*kwargs"):
        @tool(side_effect=SideEffectClass.PURE)
        def f(**kwargs: int) -> int:
            """F."""
            return 0


def test_async_tools_are_supported_by_acall():
    @tool(side_effect=SideEffectClass.PURE)
    async def f(x: int) -> int:
        """F."""
        return x
    assert f.name == "f"


def test_non_optional_union_rejected():
    with pytest.raises(ValidationError, match="Union types without None"):
        @tool(side_effect=SideEffectClass.PURE)
        def f(x: Union[int, str]) -> None:
            """F."""


def test_union_with_multiple_non_none_rejected():
    with pytest.raises(ValidationError, match="multiple non-None"):
        @tool(side_effect=SideEffectClass.PURE)
        def f(x: Union[int, str, None] = None) -> None:
            """F."""


def test_dict_non_str_key_rejected():
    with pytest.raises(ValidationError, match="str"):
        @tool(side_effect=SideEffectClass.PURE)
        def f(x: dict[int, str]) -> None:
            """F."""


def test_unsupported_type_points_to_escape_hatch():
    class Foo:
        pass

    with pytest.raises(ValidationError, match=r"schema="):
        @tool(side_effect=SideEffectClass.PURE)
        def f(x: Foo) -> None:
            """F."""


def test_empty_description_override_rejected():
    with pytest.raises(ValidationError, match="empty description"):
        @tool(side_effect=SideEffectClass.PURE, description="   ")
        def f(x: int) -> int:
            """Has docstring but override is empty."""
            return x


# ---------------------------------------------------------------------------
# Tool object semantics
# ---------------------------------------------------------------------------

def test_repr_excludes_handler():
    @tool(side_effect=SideEffectClass.PURE)
    def add(x: int) -> int:
        """Add."""
        return x

    r = repr(add)
    assert "_handler" not in r
    assert "function" not in r
    assert "add" in r


def test_equality_ignores_handler_identity():
    """Semantic-field equality; handler identity must not matter."""
    def h1(x: int) -> int: return x
    def h2(x: int) -> int: return x

    t1 = Tool(name="f", description="d",
              parameters_schema={"type": "object", "properties": {}, "required": []},
              side_effect=SideEffectClass.PURE, _handler=h1)
    t2 = Tool(name="f", description="d",
              parameters_schema={"type": "object", "properties": {}, "required": []},
              side_effect=SideEffectClass.PURE, _handler=h2)
    assert t1 == t2


def test_name_override():
    @tool(side_effect=SideEffectClass.PURE, name="custom")
    def f(x: int) -> int:
        """F."""
        return x

    assert f.name == "custom"


def test_description_override_wins_over_docstring():
    @tool(side_effect=SideEffectClass.PURE, description="Override.")
    def f(x: int) -> int:
        """Ignored."""
        return x

    assert f.description == "Override."


def test_description_is_first_paragraph_only():
    @tool(side_effect=SideEffectClass.PURE)
    def f(x: int) -> int:
        """Short summary.

        Longer body that must not appear in the description.
        """
        return x

    assert f.description == "Short summary."


def test_boolean_not_misdetected_as_int():
    @tool(side_effect=SideEffectClass.PURE)
    def f(flag: bool) -> None:
        """F."""

    assert f.parameters_schema["properties"]["flag"] == {"type": "boolean"}


def test_nested_list_dict():
    @tool(side_effect=SideEffectClass.PURE)
    def f(data: dict[str, list[int]]) -> None:
        """F."""

    assert f.parameters_schema["properties"]["data"] == {
        "type": "object",
        "additionalProperties": {"type": "array", "items": {"type": "integer"}},
    }
