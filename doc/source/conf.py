# Mock context for Sphinx autodoc
import sys
from unittest.mock import Mock, patch

# Create a mock context that always returns True for initialized
class MockContext:
    def __init__(self):
        self._initialized = True
    
    @property
    def initialized(self):
        return True
    
    def initialize(self):
        pass

# Apply the mock before any problematic imports
mock_context = MockContext()
sys.modules['simstack.core.context'] = Mock()
sys.modules['simstack.core.context'].context = mock_context

import os
import sys
sys.path.insert(0, os.path.abspath('../..'))
sys.path.insert(0, os.path.abspath('../../src'))

autodoc_mock_imports = [
    'simstack.server.simstack_server',
    'simstack.server',
]

project = 'SimStack II'
copyright = '2025, Wolfgang Wenzel'
author = 'Wolfgang Wenzel'

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.autosummary',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
    'sphinx.ext.autodoc',
    'sphinx.ext.viewcode',
    'sphinx.ext.napoleon',
]

templates_path = ['_templates']

exclude_patterns = [
    '_build',
    'Thumbs.db', 
    '.DS_Store',
    '.doctrees',
    'logs',
    'simstack_tree'
    '**/.git',
    '**/__pycache__',
    '*.pyc',
    '*.pyo'
]

# Enable autosummary to automatically generate stub files
#autosummary_generate = True

# Enable recursive generation (Sphinx 3.1+)
#autosummary_recursive = True

#autoclass_content = "both"

# -- Options for HTML output -------------------------------------------------
html_theme = 'furo'

html_theme_options = {
    "sidebar_hide_name": True,
    # Force dark mode
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
]

html_static_path = ['_static']
