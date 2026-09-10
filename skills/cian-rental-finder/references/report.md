# Cian rental HTML report contract

The final deliverable is one polished, mobile-friendly HTML report rendered in
the conversation, not a stream of listing notes. Return it as one complete
fenced `html` fragment followed by at most a short handoff sentence. Do not put
doctype, head, scripts, stylesheets, inline styles, forms or iframes in the
fragment.

## Quick nearby report override

For the Skill's quick nearby mode, this section overrides the comprehensive
selection requirements below:

- Deliver one complete mobile-friendly HTML report after inspecting one usable
  real Cian listing; one to three listing cards are valid and no Top 5 claim is
  required.
- Title it as a quick nearby report and label the principal card `真实房源样例`.
  The first screen must show the actual result, observed monthly rent and the
  number of listings inspected.
- A partial gallery review is valid when it visibly covers the living/sleeping
  area and one other material room if available. State how many photos were
  actually viewed; never imply a full-gallery audit.
- Use Cian's displayed area, metro, distance or walking text. Do not open Yandex
  Maps, do not claim an independently verified walking route and do not expose a
  private saved work address.
- Include an observed Cian HTTPS link when the app exposes one without leaving the
  bounded flow. Otherwise provide the public listing title/address label or ID
  needed to find it again and state that the direct link was not captured. A
  missing link does not demote an otherwise observed quick-mode sample.
- Use one real observed Cian interior image or PhoneBridge screenshot when
  available. If an image artifact cannot be embedded, keep the listing in the
  report and candidly state that the photo was inspected in Cian but not attached.
- End with compact coverage counts and a note that no landlord was contacted and
  no booking or payment was made. Unknown fees, lease terms or restrictions must
  remain visibly unknown.

The rest of this document defines comprehensive work-distance mode only.

## Selection before presentation

- Build a sufficiently broad, deduplicated candidate pool first. Rank only after
  the fixed-scope search and verification pass; never call the first five opened
  listings the Top 5.
- For a large accessible result pool, a fewer-than-five or zero-result report is
  valid only after at least 20 plausible distinct cards were scanned and at least
  10 of the strongest distinct candidates were opened, unless the card-level pass
  leaves fewer than 10 plausible candidates. Report both counts; one opened listing
  can never justify an empty final ranking while other candidates remain accessible.
- Select the best 5–10 fully qualified homes from that pool. Five is the normal
  minimum and ten is the useful maximum, not a quota that permits padding.
- HTML is reserved for this completed ranked deliverable. Never render a progress
  checkpoint, incomplete candidate audit or "Top 5 not ready" notice as HTML.
- If credible fixed-scope exhaustion leaves fewer than five qualified homes,
  immediately deliver the complete report with all qualified homes and add a
  visible coverage/exhaustion section. Five is a target, not a gate that may
  suppress the report. If zero qualify, deliver the same polished report with a
  clear empty-result conclusion and useful next-step tradeoffs. Never promote a
  rejected or pending lead to fill the ranking.
- Rank holistically: hard eligibility first, then interior quality and
  consistency, actual walk, recurring cost, move-in cash, commission, listing
  completeness and material restrictions. Explain the decisive tradeoff for
  every finalist. A cheaper or closer listing is not automatically better.
- Before answering, audit every finalist again against its captured link,
  photos, costs and Yandex walking result. Resolve contradictions or mark the
  listing pending and remove it from the ranked section.
- A full photo review requires entering the listing gallery and observing the
  counter advance through every image to `N/N`; a detail-page hero image or a
  stated photo count is insufficient. Record the observed `N/N` total and exclude
  candidates whose material rooms remain unseen or whose gallery pass is incomplete.

## Report structure

Use supported tags only: article, section, header, footer, div, span, h1-h3, p,
small, strong, em, ul, ol, li, a, img, br and hr. Use the design classes hero,
card, recommended, eyebrow, muted, facts, badge, button, grid and caption.
Quote attributes, self-close img/br/hr, and escape `&` in URLs as `&amp;`.

The report should contain:

1. A compact hero with the searched scope, the qualified/pending/rejected counts
   and a candid coverage statement. Do not expose the precise work address.
2. A short ordered comparison summary naming the Top 5–10 and each one's main
   differentiator. This gives the owner an immediate overview before the cards.
3. One card per ranked home. Mark rank 1 with `card recommended`; use `card` for
   the rest. Each card must show:
   - rank, observed listing/address label, monthly rent and known monthly subtotal;
   - actual Yandex walking distance and time;
   - deposit, commission, utilities, known move-in cash and whether the deposit
     is refundable when stated;
   - term, availability and material tenant/pet restrictions when shown;
   - one representative real interior image and an honest source caption;
   - a concise reason it ranks here plus any material caveat;
   - the observed Cian listing link and observed Yandex place/route link as
     separate tappable buttons when available.
4. A compact methodology/coverage section: distinct listings enumerated, opened,
   route-checked and fully photo-reviewed, result areas covered, plus categorized
   rejection and pending counts. List only decision-relevant pending exceptions;
   do not dump every rejected card into the main report.
5. A final note that no landlord was contacted and no booking or payment was made.

## Photos, links and writing quality

Use one representative real image per finalist, normally 5–10 images total and
never more than twelve. Use an observed public HTTPS photo URL or the exact
`artifact_image_uri` returned by PhoneBridge. A PhoneBridge capture must show the
individual listing's interior photo page and be captioned as a Cian screenshot;
do not call it official photography. Never use generated, stock, unrelated or
remembered imagery.

Every Cian and Yandex link must have been observed for that exact finalist. Do
not construct a URL from an address or use a generic search link as if it were a
listing/route. If a required source link or representative real photo is missing,
the home is pending and cannot enter the ranked section.

Keep the report attractive, practical and concise: short headings, comparable
fact order, no repeated prose and no unsupported superlatives. Say "best among
the verified pool" rather than claiming the absolute best in the city. The first
screen should reveal the result and number of qualified homes, not just a large
decorative title. Validate that the HTML fence is complete and within the mobile
poster limits before sending it. Inspect the rendered result, not only its source:
confirm that every selected photo loads and belongs to the right listing, Cian and
Yandex buttons map to that same home, text is readable without awkward overflow,
and the ranking is immediately visible. Keep photo-navigation and tool-call details
out of the report; present only the useful result and a compact confidence/coverage
summary.
