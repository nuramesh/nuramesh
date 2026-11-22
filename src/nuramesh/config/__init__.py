"""Loading configuration files (YAML and JSON) into plain data for the core models (decision D1).

Core accepts dicts; this package turns text into dicts safely. YAML needs the `nuramesh[yaml]`
extra; JSON needs nothing.
"""

from nuramesh.config.loader import ConfigError, load_config_file, parse_json, parse_yaml
from nuramesh.config.templates import (
    load_template_versions,
    save_template_file,
    template_from_yaml,
    template_path,
    template_to_yaml,
)

__all__ = [
    "ConfigError",
    "load_config_file",
    "load_template_versions",
    "parse_json",
    "parse_yaml",
    "save_template_file",
    "template_from_yaml",
    "template_path",
    "template_to_yaml",
]
