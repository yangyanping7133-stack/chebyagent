# General Agent and Scenario Skill Boundary

Status: implementation baseline

Date: 2026-08-09

## Two product layers

ChebyCodex Standalone has exactly two product layers:

1. **General Agent Appliance**: the Android conversation UI, Codex runtime, PhoneBridge/Node tools,
   threads, attachments, generic rich-message rendering, local interaction state, confirmation, and
   recovery.
2. **Scenario Skills**: bounded instructions for a user goal and the Android app or tool surface
   needed to complete it. A Skill owns package names, page recognition, interaction order, recovery
   paths, result fields, uncertainty rules, and confirmation boundaries.

The Agent Appliance must not contain knowledge of Russia, maps, taxis, rentals, menus, hotels,
barbers, pharmacies, or clinics. A scenario Skill may request general UI primitives, but it may not
introduce a scenario-specific renderer.

## Reusable TaskResponse primitives

The general final-turn contract provides these presentation choices:

- `text`: readable explanations and summaries;
- `gallery`: bounded reference images with provenance/uncertainty labels;
- `comparison`: comparable candidates and visible facts;
- `selection`: grouped selectable items, local quantity changes, review, total, and presentation;
- `bilingual`: source-language and Chinese handoff content.

Codex chooses a primitive from the user's goal, not from whether the input happened to be text or
an image. Tool use remains unrestricted during the turn; the schema constrains only the final
TaskResponse.

## What the twelve acceptance cases are for

The twelve cases in `docs/product/RUSSIA_STARTER_PACK_1_0.md` are an engineering convergence suite,
not twelve branches in the Agent. Every failed case must produce one of only two changes:

1. add or repair a business-agnostic Agent/UI primitive used by multiple scenarios; or
2. add or repair the responsible scenario Skill.

If a proposed fix adds an app name, Russian workflow, or domain rule to the renderer, gateway, or
conversation core, it violates this boundary even when the individual case passes.

| Case family | General capabilities exercised | Skill ownership |
| --- | --- | --- |
| A1 taxi | app control, comparison, confirmation, resume | Yandex Go ride Skill |
| A2-A3 routing | OCR input, comparison, bilingual verification, app control | Yandex Maps route Skill |
| B1 hotel | comparison, evidence labels, confirmation | selected hotel-app Skill |
| B2 rental | filters, comparison, app control, confirmation | Cian rental Skill |
| B3 landlord | OCR/text input, bilingual handoff, confirmation | landlord communication Skill |
| C1 local search | comparison, app control, route handoff | Yandex Maps local-search Skill |
| C2-C3 dining | OCR input, gallery, selection, bilingual handoff | Russia menu Skill |
| D1 barber | comparison, app control, message/booking confirmation | local-service booking Skill |
| D2 pharmacy | OCR input, local search, bilingual handoff, safety boundary | pharmacy availability Skill |
| D3 clinic | comparison, bilingual handoff, safety and booking boundary | clinic visit Skill |

Several cases can share the same app but still use different Skills when their recognition,
safety, result, or confirmation workflow differs. Conversely, closely related cases may share one
Skill when the workflow boundary is genuinely the same, such as menu selection and dietary
handoff.

## Skill contract

Each production scenario Skill must define:

- trigger and non-trigger conditions;
- preferred Android package/tool surface and web-fallback policy;
- observable page states and recovery steps;
- required result evidence and uncertainty handling;
- TaskResponse primitive and fields to populate;
- actions that are reversible versus actions requiring explicit confirmation;
- claims it must never make;
- at least one acceptance case that proves the Skill without adding domain code to the Agent.

The first bundled reference implementation is `skills/russia-menu-assistant`. Other Skills are
created when their anchor case is implemented, avoiding empty placeholders that claim untested
capability.

## Acceptance rule

A case passes only when it works through a natural user prompt or attachment on the target phone,
uses the intended Android app when one is available, returns a useful generic TaskResponse, stops
before the consequential action, and preserves the conversation after the app handoff. Static
schema or screenshot-only demonstrations are component checks, not an end-to-end pass.
