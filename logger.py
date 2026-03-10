"""Logging configuration for MAPS Cafe Bot."""

import logging
import sys


def setup_logger(name: str) -> logging.Logger:
    """Set up and return a logger with consistent formatting.

    Args:
        name: The name for the logger (typically __name__).

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(name)

    # Only configure if not already configured
    if not logger.handlers:
        logger.setLevel(logging.INFO)

        # Console handler
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.INFO)

        # Format: timestamp - level - name - message
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(name)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)

        logger.addHandler(handler)

    return logger
