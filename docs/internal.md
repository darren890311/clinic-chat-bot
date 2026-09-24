# Appointment assistant: internal notes

For the team evaluating this build. It covers the architecture, what it cost to
build and what it would cost to run in production, the choices made along the
way, and the security features implemented.

---

## 1. The decision everything else rests on

**The language model never decides when an appointment happens.**

It listens, works out what the patient is asking for, and then asks the
scheduling system. Every question with a consequence is answered by ordinary
logic with no AI in it. Is this time free. Does this dentist perform this
treatment. How long does it take. Does it still fit once the previous
appointment is allowed to overrun.

This is not a rule the model has been asked to follow. **The model has no way
to complete a booking at all.** It can offer times and reserve one for a few
minutes; the appointment is only made when the patient fills in a short form on
their screen and submits it. Nothing within the model's reach can do that step.

This matters because of the way these models fail. They do not crash. They
agree with you. A patient who insists three times that nine o'clock is free
and that the assistant should just book it will, with most systems, eventually
be told that it is booked.

Here that cannot happen, because there is no button for the assistant to press
however thoroughly it is persuaded. Tested by insisting four times in a row, it
pointed back at the form four times. The worst outcome of a confused
conversation is a patient offered a time they decline, rather than a patient
who arrives to find no appointment.

It also makes the AI genuinely replaceable. Switching provider changes the
wording of replies; it cannot change which slots exist, who is qualified, or
what gets booked.

---

## 2. The architecture

Five parts. The arrows show what a booking actually does.

```
   Patient
  (types or speaks)
       |
       v
  +--------------------------------------------------+
  |  Assistant                                        |
  |  understands the request, asks the scheduler,     |
  |  reads back what it is told. Decides nothing.     |
  +--------------------------------------------------+
       |                                    ^
       | "what is free on Thursday?"        | "these times"
       v                                    |
  +--------------------------------------------------+
  |  Scheduler                                        |
  |  working hours, treatment lengths, who does       |
  |  what, turnaround gaps, daylight saving           |
  +--------------------------------------------------+
       |                    |
       | existing bookings  | when are the dentists
       v                    v  already busy?
  +------------------+   +----------------------------+
  |  Practice        |   |  Dentists' own calendars   |
  |  records         |   |  Google / Outlook          |
  +------------------+   +----------------------------+
       ^
       | the patient submits the form
       |
  +--------------------------------------------------+
  |  Confirmation form: the only thing that books     |
  +--------------------------------------------------+
```

Four things are deliberately interchangeable: the AI provider, the speech
recogniser, the voice, and the calendar system. Each sits behind a fixed
interface, so adding a third calendar system is one new adapter and a
configuration change, with nothing touched in the scheduler, the assistant or
the screens.

That is demonstrated rather than intended. **Two AI providers are
implemented and both work**, Anthropic's Claude and OpenAI's GPT. A test feeds
the same conversation to both and asserts the results are identical once
normalised, while also asserting that what each one sent over the wire differs.
Changing provider is one setting. **Two calendar systems are implemented and
both have been operated against real accounts**, Google and Microsoft. Speech
follows the same pattern, with one working provider and one deliberately
silent one. The silent one is used when no key is configured, so a missing key
switches the microphone off rather than failing when the button is pressed. Recognition and the voice
are separate settings, because they are separate purchases: a practice might
want a cheap transcriber and a good-sounding voice.

The practice's own records are the single source of truth. The dentists'
calendars are read so the assistant never offers a time a dentist has already
committed elsewhere, and confirmed appointments are written back into them. If
a calendar service is unreachable the assistant says it cannot check
availability rather than guessing. It will not offer a time it has not
verified.

### What the practice offers

The brief specifies five treatments by letter and duration; the names are ours.

| | Treatment | Length | Who performs it |
|---|---|---|---|
| A | Routine Cleaning | 1 hour | all three dentists |
| B | Dental Examination | 1 hour | all three dentists |
| C | Root Canal Treatment | 2.5 hours | senior dentists only |
| D | Crown Fitting | 2 hours | senior dentists only |
| E | Full Mouth Restoration | 6 hours | senior dentists only |

Two of the three dentists are senior. Fifteen minutes is left between
appointments, so a one-hour treatment occupies 75 minutes of a diary.

*For readers who want the stack:* Python and FastAPI, PostgreSQL, a Vue front
end, deployed as a single container on Google Cloud Run with a hosted database.
One deployment target, one web address, no separate front-end hosting.

### What the architecture is for

Three properties follow from the shape above, and each exists because of a
specific way this goes wrong.

**Two patients cannot take the same slot.** Application code that checks "is
this free?" and then writes the booking has a gap between the two steps where
another request can slip in. Three things close it, in order.

Requests for the same dentist queue rather than run side by side, so the second
one waits for the first to finish and is then told the time has gone. That is
why it is turned away before it writes anything at all. If it does write, the
database itself refuses overlapping appointments for the same dentist and the
write fails.

Correct application logic is the first line and the queue is the second. The
database constraint is the one that does not depend on either of them being
right.

**A patient who walks away mid-conversation loses nothing.** Choosing a time
reserves it for five minutes rather than booking it, and the countdown is on
screen. An abandoned reservation lapses on its own, and the time returns to the
pool without anyone having to release it. Moving an appointment is a single operation rather
than a cancel followed by a rebook. The original time stays until the new one
is confirmed, so someone who abandons a change keeps what they arrived with.

**The scheduling rules can be tested exhaustively.** The part that decides what
is free touches no database, no network and no clock. The current time is
passed in, so the same inputs always give the same answer. That is what makes
it possible to test a daylight saving changeover, a case that happens twice a
year and would otherwise be verified by waiting for it.

---

## 3. Cost

### Building it

**Two days, one engineer, working with an AI coding assistant.** That produced
7,484 lines of application code and 3,596 lines of tests. The 165 tests cover
the scheduling rules, the database guarantees, the isolation between practices,
and both calendar integrations.

What is missing matters more than the number. What exists is a working
application, operated end to end against real Google and Outlook calendars.
What does not exist is the work separating that from something a practice can
be handed: **another 19 to 30 engineer-days**, itemised in section 6. The two
items there with no estimate are larger projects rather than finishing work.

One item there is not engineering time at all: Google requires a review before
an application asking for calendar access can be used by anyone outside a short
test list. That is weeks of waiting, and it has to be started early.

### Running it in production

Per-conversation cost is only meaningful with a volume to multiply it by, so
the volume is derived from the practice rather than assumed.

**Capacity.** Three dentists, Monday to Friday nine to six plus Saturday
mornings, fifteen minutes between appointments. At full occupancy, and assuming
every appointment were the shortest treatment offered, the practice fits about
**507 appointments a month**. That is a deliberate ceiling, since a six-hour
restoration occupies far more, so a realistic mix sits well below it.

**Occupancy is an assumption, and is presented as one:**

| Occupancy | Bookings/month | Typed conversations | All by voice |
|---|---|---|---|
| 40% | 203 | $12 | $18 |
| 55% | 279 | $17 | $24 |
| 70% | 355 | $21 | $31 |
| 85% | 431 | $26 | $37 |

A typed booking conversation costs about **6 cents** in AI usage, measured
from the tokens a real booking conversation actually used, with caching on. The
voice figure of about **9 cents** is an estimate rather than a measurement. It
takes list prices for recognition and synthesis and assumes a six-turn
conversation. Most of the addition is the synthesised voice rather than the
recognition. Reality sits between the two columns.

**What it costs today is about a dollar a month.** The server scales to
nothing when idle, the database is on a free tier, nothing is monitored, no
domain has been bought and no text messages are sent. Four of the six lines
below do not exist yet. Everything spent so far has been the AI itself, and
across all development and testing that is under ten dollars.

**A full production month, at roughly 70% occupancy, is a different figure,
and most of the difference is work that has not been done:**

| | Monthly | In place today |
|---|---|---|
| AI and speech | $21–31 | yes |
| Server, scaling to nothing when idle | under $1 | yes |
| Database with backups and point-in-time recovery | $20–30 | no, free tier |
| Error tracking and uptime monitoring | $0–25 | no |
| Domain name | about $1 | no |
| Text messages for patient verification, once built | $10–25 | no |
| **Total** | **roughly $55–110 a month** | |

The database line is the one that matters most. A free tier is right for a
demonstration and wrong for a practice's appointment book, because a paid tier
is what buys backups and the ability to restore to a point in time.

**The bill is measured, not estimated.** Every conversation keeps a running
total of the tokens it has spent, and one request returns the total for any
period, in tokens and, once a price is configured, in money. Without one it
reports tokens alone, because a price that has gone stale would be quoted as
fact.

That was added because the question came up during development and had no
answer. The provider returns a token count with every reply, and the
development build shows it in the corner of the screen, but nothing was
storing it. Working out what a
week of testing had cost meant counting rows in the database and multiplying by
a price measured once, which gives a range. A practice asking what last month
cost should get a figure.

**Maintenance is people, not machines.** The things that will genuinely need
attention are the calendar providers occasionally changing their interfaces,
better and cheaper AI models worth moving to, and the practice asking for
something the current scope excludes. **Budget one to two engineer-days a
month**, rising around any change the practice requests.

---

## 4. Choices made along the way

**A more capable, more expensive AI model.** The model's hardest job is not
scheduling, which it never does. It is recognising, from a patient's own words,
that someone has knocked out a tooth and needs to be seen today rather than
next week. That is not the place to save ten dollars a month. The cheaper model
is a single configuration change and roughly halves both the cost and the wait;
the reason to revisit it is speed rather than money.

**Reserve, then confirm, rather than booking in one step.** It costs the
patient an extra action and buys the guarantee in section 1: nothing is booked
without a deliberate human action on details the patient can read.

**Push-to-talk, not a continuous conversation.** The patient presses a button,
speaks, releases. Natural back-and-forth with interruption is a substantially
larger piece of engineering and solves a problem this practice does not yet
have. Timed end to end, one spoken turn takes eight seconds: one and a half to
recognise the speech, four and a half for the assistant to answer, two to
synthesise the reply. The written reply appears at six seconds and the audio
follows. That is acceptable when someone has just pressed a button and expects
a wait, and would not be on a live telephone call. That is the honest reason a
telephone version is a later project rather than a small addition.

**Everything the patient hears is also on screen.** Speech cannot be re-read,
so what the recogniser heard is shown, and can be corrected, before anything
acts on it. Phone numbers are always typed: digits are where recognition fails
most often and where a mistake is least recoverable.

**Availability presented by time, not by dentist.** Asked for a root canal, an
early version offered "2 PM with Dr. Okafor" while Dr. Hale was equally free at
2 PM. Every word was true, and the patient reasonably read it as Dr. Hale being
busy. The cause was the shape of the information the assistant was given, not
its wording, so the fix was to organise availability by time. That makes the
honest answer the easy one to give.

---

## 5. Security features implemented

**One practice cannot see another's data.** Records are fenced off inside the
database itself, not by the application remembering to filter. There is a
well-known trap here: these rules are silently ignored for administrative
database accounts, so a system can appear to protect data while protecting
nothing. The application connects with a restricted account, and the tests
refuse to run until they have confirmed that the account under test really is
restricted.

**No credential appears in the code or in the deployment.** Secrets are held in
a managed secret service and handed to the application as it starts, and the
deployment pipeline authenticates with no long-lived key at all. The single
long-lived credential in the system is the speech provider's key, restricted to
that service and expiring on a fixed date.

**Calendar access is as narrow as each provider allows.** For Google, the
system asks only for permission to see whether a dentist is busy and to manage
its own appointments. It cannot read what a dentist's other meetings are about
or who is attending them, and a dentist can see exactly that on the consent
screen before agreeing. Microsoft offers no equivalent narrow permission. There
the same guarantee rests on our software rather than on the permission itself.
The software has no way to read event contents either, but the distinction is
real and worth recording.

**Calendar credentials are encrypted before storage.** A copy of the database
does not give anyone access to a dentist's calendar.

**Text written by other people never reaches the AI.** A dentist's calendar
contains meeting titles and notes written by whoever sent the invitation. That
is text from outside the practice, and text from outside is exactly how these
models are manipulated. An event titled with instructions for an assistant is
a real technique. The software has no way to read an event's title, description
or attendees; it can only ask whether a period is busy. There is nothing to
sanitise because nothing is fetched.

**A conversation can only act on appointments it was given.** Cancelling or
moving an appointment requires that this conversation either made the booking
or looked it up with the name and number it was booked under. A reference
obtained any other way is refused, and refused in exactly the same words as an
appointment that does not exist, so the refusal cannot be used to discover
which references are real.

**A patient is shown nothing about the machinery.** Which AI vendor answered,
which model, and how much of the prompt came from cache are a developer's
instrument, and they sat in the corner of every patient's screen. They are
shown in development now, and on a deployed copy only when the address asks
for them. A patient booking a filling has no use for the model name, and
nobody outside should have to be told which vendor to study.

**There is no clinical information in the system.** Names, phone numbers and
email addresses only: no history, no diagnoses, no notes, no payment details.
This is what limits any leak. The worst thing anyone can learn is that a named
person has a dental appointment on Thursday. Adding a field adds to that
sentence.

**The assistant will not give clinical advice, and knows when to stop.** It
does not assess symptoms, quote prices, or discuss insurance. Where symptoms
suggest something a dentist cannot treat, such as swelling spreading towards
the eye, difficulty breathing, or bleeding that will not stop, it stops
booking, gives the practice's number, and hands over.

### What is deliberately not protected

**Nothing verifies that a caller is who they say they are.** To find an
existing appointment the assistant asks for the name and phone number it was
booked under. Both are checked; neither is a secret. Somebody who knows both
can see when a patient is coming in and with whom, and can cancel it.

This is worth comparing with what the practice does today. Ring any dental
surgery, give a name and a number, and the receptionist will tell you the
appointment and cancel it if asked. The risk is not new. What changes is how often it can
be tried. Someone guessing their way in gets one attempt per telephone call,
and a receptionist starts to wonder by the third. A web address takes any
number of attempts, from anyone, at any hour, and wonders nothing.

Closing it requires sending a one-time code to the phone number before anything
is disclosed, which needs text messaging. **That is the important point about
leaving text messaging out: it was a security decision, not a feature
decision.** Listed among things not yet built it reads as missing appointment
reminders. What it actually decides is that nothing authenticates a patient.

---

## 6. Before this goes live

None of these are oversights. Each has a reason for being out of scope, and a
point at which it stops being optional.

| | Why it is not here | When it becomes necessary | Effort |
|---|---|---|---|
| Verify the patient by text message | Needs a messaging provider and a cost per message | Before real patient data is in the system | 3–4 days |
| Limits on request rate | No abuse in a controlled trial | Alongside the row above, and much the cheaper half of it | 1–2 days |
| Staff login and practice screens | The scheduling and safety work mattered more | Before anyone but the developer connects a calendar | 5–8 days |
| Monitoring, alerting, backups | A free database tier is right for a demonstration | Before a real appointment book depends on it | 3–5 days |
| Accessibility and browser testing | Two browsers were used in development | Before patients outside the practice use it | 2–3 days |
| Google's production review | Test-list access is enough to demonstrate | Before the public can connect calendars | 2–3 days, plus weeks of waiting |
| Hardening and a staff guide | Finishing work | At handover | 3–5 days |
| Continuous voice conversation | An eight-second pause is fine for push-to-talk | If this is to answer the telephone | not estimated |
| Multi-practice administration | The data layer already separates practices; the screens do not | When a second practice is taken on | not estimated |

The system has been operated end to end against real Google and Outlook
calendars, with three dentists in three different states: one on Google, one on
Outlook, and one with no calendar connected. That last case behaves correctly:
a dentist's own bookings still block their time, and only outside commitments
are invisible. The remaining work above is operational rather than structural.
