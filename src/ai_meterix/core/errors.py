"""Errors raised by AI Meterix."""


class AiMeterixError(Exception):
    """Base class for every error AI Meterix raises on purpose."""


class ValidationError(AiMeterixError, ValueError):
    """A value does not fit the data contract (record 0010)."""


class ConfigError(AiMeterixError):
    """The configuration file or environment is invalid."""


class PluginError(AiMeterixError):
    """A plugin is missing or failed to load."""
