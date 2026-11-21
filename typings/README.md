# detect-secrets 1.5.0 annotations

These stubs fill two missing type arguments in the pinned library. They describe the existing runtime API and do not replace its implementation.

- [settings.py at v1.5.0](https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/settings.py) defines `default_settings` as a context manager. `get_plugins` constructs a list with `from_plugin_classname`, which returns `BasePlugin` instances.
- [plugins/base.py at v1.5.0](https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/base.py) defines `secret_type` and the compiled string patterns in `RegexBasedDetector.denylist`.

Pyright loads these stubs from its default `typings` directory. Remove them when the dependency supplies complete annotations.
