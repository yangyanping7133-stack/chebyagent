# Coffee poster delivery

Return one fenced `html` fragment as the main answer: a short headline, a prominent
first recommendation and compact alternatives. The model chooses layout/content;
the phone supplies responsive type, warm colors, photo cards and buttons.
This release displays HTML. Image export is optional when requested; do not
promise a PNG without an actual export artifact.

Each finalist needs its exact observed name, representative real photo or honest
photo-page screenshot, a brief reason, walking duration/route summary, rating
plus review count when available, and the actual branch link. Only admit candidates
with verified real photos and credible strong ratings; a missing-photo disclaimer
does not make a venue eligible. Do not fill alternative cards with clearly weak
ratings such as the rejected 4.3 example just to reach a count. One qualified venue
is enough. Keep reasons to one or two sentences. Mark other missing information
instead of inventing it.

Distinguish rating counts from written reviews; do not label both simply as reviews.
Keep source captions short. Omit nonessential photo dates; if included, verify the
displayed day/month rather than translating a leading day number into a month.
A route summary should add an observed street, landmark or direction from the
actual preview, not repeat only the endpoints and duration. If the preview does
not establish those details, mark that gap rather than inventing directions.

Allowed tags: article, section, header, footer, div, span, h1-h3, p, small,
strong, em, ul, ol, li, a, img, br, hr.
Design classes: hero, card, recommended, eyebrow, muted, facts, badge, button,
grid, caption. Combine classes with spaces. No inline style/scripts/iframes/forms.
Quote attributes, self-close img/br/hr and escape URL query separators as `&amp;`.
Do not include doctype/head wrappers or raw ampersands.

Use a brief hero headline, without repeating the first venue's name or a long
intro. Then use `section class="card recommended"` for the first choice and
`section class="card"` for alternatives. Within a card put name and walking/rating
facts before the photo, then one short reason and the Maps button. The first screen
should convey the choice and walking effort, not just a large title and image.
Keep the hero title short enough for one line where practical; omit a separate
intro paragraph if the same facts already appear in the first card. A headline
must not turn limited sampling into a claim that no nearer venues exist.
Put access facts in `p class="facts"`, actual place links in
`a class="button"`, and source/uncertainty in `small class="caption"`.
Wrap each button and its following caption in separate `p` elements so the
caption does not run inline beside the button in a narrow mobile card.

Use observed public HTTPS photo URLs or the exact `artifact_image_uri` returned
by PhoneBridge as img src. The latter references a private on-phone screenshot,
not an upload. Do not invent filenames, expose private screen contents, or claim
a screenshot is clean official photography. At most six images, with concise
alt text; usually one per venue. A failed photo load must not hide the place link.

Use observed Yandex Maps share/branch URLs, not generic search links masquerading
as exact locations. A route link is a bonus; verified walking time/description
and the exact place link suffice. Never invent route geometry. Navigation opens
only when the user taps, never automatically from an answer.
