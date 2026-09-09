# ACE core provenance

Source: https://github.com/kayba-ai/agentic-context-engine
Commit: 321d430e520f369315bad512cd2d90f1fa14a596 (0.12.0 source tree)
License: Apache-2.0, retained in LICENSE.

`skillbook.py` and `insight_source.py` are unmodified copies of `ace/core/`.
The local package initializer deliberately exposes only these dependency-free
modules. The mobile adapter uses upstream Skillbook and UpdateBatch, not the
full ACE runner, hosted service, LiteLLM or automatic Reflector/SkillManager.
The existing Codex model performs reflection/curation through the local tools.
Cheby adds scoped storage, concurrency checks, revision history and rollback.
