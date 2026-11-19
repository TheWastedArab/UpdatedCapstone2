# Task cards

Give the participant one card at a time. The facilitator's notes (in italics) are not read out.

---

## Task 1 — Create an account (limit 3 minutes)

> The Bug Tracker extension is installed in this VS Code window. Create an account for
> yourself with the email address on this card and a password of your choice, and get to the
> point where you are signed in.
>
> Email: `p<N>@study.example.com`

*Success: the participant is signed in and sees the bug list (empty or "no project" message).
Then pause the clock and run `add_participant.py`; ask the participant to press Refresh.*

---

## Task 2 — Report your first bug (limit 4 minutes)

> While using the Demo Shop app you noticed that the checkout page shows the total price
> without tax when the cart has more than ten items. Report this bug in the Demo Shop project.

*Success: the report is submitted and its detail page (with the assigned severity) is shown.
Task 1 + Task 2 time is the NFR-11 measure. If a duplicate warning appears, any choice
that ends in a submitted report counts.*

---

## Task 3 — Find a bug (limit 2 minutes)

> Someone mentioned a bug about the login page that is still open. Find it and open it.

*Success: the detail page of "Login page rejects a correct password…" (filed by `--setup`) is shown.
Note whether the participant used search, a filter, or scrolled.*

---

## Task 4 — Comment and watch (limit 2 minutes)

> On the bug you just found, add a comment saying "I can reproduce this on Firefox", and make
> sure you are told when it changes.

*Success: the comment is visible on the detail page and the Watch toggle is on.*

---

## Task 5 — Check your notifications (limit 2 minutes)

> *(Before this card the facilitator runs `add_participant.py --status`, which moves the
> watched bug to "In Progress".)*
>
> Find out whether anything has happened to the bugs you are following.

*Success: the participant opens the notifications and reads out the status change.*
