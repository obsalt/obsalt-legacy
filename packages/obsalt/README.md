# obsalt

Core package for obsalt — a self-hosted call analytics and quality system for AI voice agents.

This package ships **no providers**. Install a source plugin against the public
`obsalt.plugins` entry-point group:

```bash
pip install "obsalt[vapi,retell]"
```

Plugins are trusted, operator-installed code. They are pinned and inventoried;
they are not a security sandbox. See `docs/plugins.md`.
