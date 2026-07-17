# Mock context for Sphinx autodoc
import sys
from unittest.mock import Mock
import types

# Create a mock context that always returns True for initialized
class MockContext:
    def __init__(self):
        self._initialized = True

    @property
    def initialized(self):
        return True

    async def initialize(self):
        pass

# Apply the mock before any problematic imports
mock_context = MockContext()

# Use a real module object (ModuleType) instead of Mock() to satisfy autodoc expectations
_context_mod = types.ModuleType("simstack.core.context")
_context_mod.context = mock_context
sys.modules["simstack.core.context"] = _context_mod

import os
import sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.abspath('../..'))
sys.path.insert(0, os.path.abspath('../../src'))

# Mock imports that are optional / not available in the docs env
autodoc_mock_imports = [
    "motor",
    "sqlmodel",
]

project = 'SimStack II'
copyright = f'{datetime.now(timezone.utc).year}, Wolfgang Wenzel'
author = 'Wolfgang Wenzel'
source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
if source_date_epoch:
    docs_built_at = datetime.fromtimestamp(int(source_date_epoch), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
else:
    docs_built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.autosummary',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
    'sphinx.ext.todo',
]

templates_path = ['_templates']

exclude_patterns = [
    '_build',
    'Thumbs.db',
    '.DS_Store',
    '.doctrees',
    'logs',
    'simstack_tree',
    '**/.git',
    '**/__pycache__',
    '*.pyc',
    '*.pyo',

    # Exclude any API-doc pages (rst) generated for tests
    'simstack/tests*.rst',
    'simstack/tests/**',
    'tests*.rst',
    'tests/**',
]

# -- Options for HTML output -------------------------------------------------
html_theme = 'furo'

html_theme_options = {
    "sidebar_hide_name": True,
    "dark_css_variables": {
        "color-background-primary": "#132738",
        "color-background-secondary": "#1e3a52",
        "color-foreground-primary": "#ffffff",
        "color-foreground-secondary": "#bac8d3",
        "color-brand-primary": "#22d5ff",
        "color-brand-content": "#0088ff",
    },
}

html_css_files = [
    'cobalt2-theme.css',
    'docs-build-badge.css',
]

html_js_files = [
    ('docs-build-badge.js', {'data-docs-built-at': docs_built_at}),
]

# If you don't have a _static folder, either create it or set this to []
html_static_path = ['_static']
