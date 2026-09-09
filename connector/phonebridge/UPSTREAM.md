# Reviewed PhoneBridge source

`phonebridge.mjs` is the reviewed extraction from ChebyNode commit `8f9ab8f`
(`codex/turkey-codex-container`). Its SHA-256 is pinned beside the file and is
validated statically, during image build, and again before process launch.

Updates must first pass the ChebyNode PhoneBridge tests and code review, then
replace this file and its hash together. The production container never bind
mounts executable PhoneBridge source from the host.
