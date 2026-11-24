# detect-secrets 1.5.0 annotations

These stubs fill two missing type arguments in the pinned library. They describe the existing runtime API and do not replace its implementation.

- [settings.py at v1.5.0](https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/settings.py) defines `default_settings` as a context manager. `get_plugins` constructs a list with `from_plugin_classname`, which returns `BasePlugin` instances.
- [plugins/base.py at v1.5.0](https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/base.py) defines `secret_type` and the compiled string patterns in `RegexBasedDetector.denylist`.

Pyright loads these stubs from its default `typings` directory. Remove them when the dependency supplies complete annotations.

## LangGraph 1.2.24 annotations

The graph stubs describe the synchronous API this project uses from pinned
LangGraph 1.2.24. Its namespace package does not expose graph modules as typed to
Pyright, and its public node, compile and invoke signatures contain unbound
generics. These declarations keep the application under strict checking without
casts or suppressed diagnostics.

The signatures follow langgraph/graph/state.py and langgraph/pregel/main.py in
the installed distribution. They cover dictionary state updates, dataclass run
contexts, string channel versions for SQLite, version v1 and v2 invocation results,
and version v2 stream updates and task events, with subgraphs, used to identify the
current invocation's path entries and the stage each task belongs to.
The update_state declaration includes as_node for dispatch recovery in #64.
A compiled subgraph with no run context can be a node, and a graph without a
run context omits context_schema, for the implement stage in #235. get_state
takes subgraphs so a pause inside implement reports the attempt it reached.
They deliberately expose only the used API. Add declarations from upstream when
adding another graph operation. Recheck these signatures when upgrading LangGraph.

Pyright checks both src and typings. The delivery, checkpoint resume, publication,
parent slicing and human/quota interrupt tests exercise the declared operations
against the real library. The stubs do not replace runtime code.
