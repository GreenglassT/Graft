"""Static-site generator for the Graft docs wiki (docs/wiki -> graftipa.com/wiki/)."""


class WikiError(Exception):
    """A build problem. The message starts with the source path it concerns."""
