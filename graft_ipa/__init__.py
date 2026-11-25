"""graft: end-to-end iOS tweak injection and sideload pipeline."""

__version__ = "2.1"

#: Every HTTP request this package makes identifies itself the same way, so a
#: server operator who sees it can find the project and the version.
USER_AGENT = f"graft/{__version__} (+https://github.com/GreenglassT/Graft)"
