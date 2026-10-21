"""Collect owned parser contracts against checkout source, not installed state."""
import importlib.util
from pathlib import Path

from kicad_route import engine


def owned_tests(name):
    path = engine.SOURCE_ROOT / 'tests' / (name + '.py')
    spec = importlib.util.spec_from_file_location('owned_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.parser.__file__).resolve() == engine.SOURCE_ROOT / 'py_router/kicad_parser.py'
    return module


ParserCacheTests = owned_tests('test_parser_cache').ParserCacheTests
LegacyTokenTests = owned_tests('test_parser_legacy_tokens').LegacyTokenTests
