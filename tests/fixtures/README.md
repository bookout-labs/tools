# Redaction detector

Foreman pins detect-secrets v1.5.0. The in-memory scan uses the plugins
returned by [`default_settings()` and `get_plugins()`][settings]. The
[`BasePlugin` API][base] exposes the kind and the raw matches. Foreman uses
the detector output locally and does not request credential verification.

- The [entropy plugins][entropy] expose an unquoted search mode. Using it
  with `analyze_line()` keeps the upstream thresholds, which the debug
  [`scan_line()`][scan] fallback skips.
- The [GitHub][github] and [GitLab][gitlab] patterns capture a prefix or
  partition ID in some alternatives. Foreman takes the complete match from
  those published patterns. The [Artifactory][artifactory] patterns also
  match delimiters, which Foreman leaves in the message.
- The [JWT plugin][jwt] verifies the header and payload. Foreman includes
  the signature segment in the replacement. The [private key plugin][key]
  matches armor headers. Foreman includes the corresponding block.
- The [keyword plugin][keyword] uses one search per pattern. Foreman uses
  those same patterns and capture groups to find repeated matches on a line.
- Credential-shaped test values are synthetic.

[settings]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/settings.py
[base]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/base.py
[entropy]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/high_entropy_strings.py
[scan]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/core/scan.py#L109-L137
[github]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/github_token.py
[gitlab]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/gitlab_token.py
[artifactory]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/artifactory.py
[jwt]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/jwt.py
[key]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/private_key.py
[keyword]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/keyword.py
