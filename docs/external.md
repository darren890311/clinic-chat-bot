# Your appointment assistant

A guide for the practice. The first half describes what it does. The second
half is how to set it up, including the parts that happen outside the
application itself.

---

## What it is

A page on your website where patients book their own appointments, by typing or
by speaking. It works the way your receptionist works: it asks what treatment
they need, finds times that genuinely suit, holds one while they decide, and
lets them confirm it themselves.

It is available at any hour. Most people who want to book a dental appointment
think of it in the evening, when nobody is at the desk.

### What it does

**It knows your diary, not just its own.** Before offering any time it checks
each dentist's own calendar. A conference, a school run, a hospital visit: if
it is in the calendar, the assistant will not offer that slot. It cannot see
what the commitment is, only that the time is taken.

**It knows who does what.** Routine cleanings and examinations are offered with
any of the three dentists. Root canals, crowns and full restorations are
offered only with the senior dentists. A patient cannot book a treatment with
someone who does not perform it.

**It knows how long things take.** A six-hour restoration is not squeezed into
an afternoon slot, and fifteen minutes is left between appointments so that one
running late does not push the whole day over.

**It moves and cancels appointments too.** A patient who wants a later time
gets the new one held before the old one is released, so they never end up with
neither. Somebody who comes back later gives the name and phone number on the
booking, and the assistant finds it.

**It knows when to stop.** It does not give clinical advice, quote prices, or
discuss insurance. If somebody describes swelling spreading towards the eye,
difficulty breathing, or bleeding that will not stop, it stops booking, gives
them your number, and tells them to ring you now.

Severe toothache is treated as a reason to be seen quickly, not as a reason to
send someone to hospital. It will search from today rather than next week and
say why.

### What it does not do

It will not take payment, send reminders, or store any clinical information. It
holds a patient's name, phone number and email address, and nothing else. There
are no notes, no history, no diagnoses.

It does not replace your front desk. It handles the ordinary booking, which is
most of the volume, and hands anything unusual to you.

---

## What a patient sees

They open the page and the assistant greets them. They can type, or press
**Speak** and talk.

If they speak, what the assistant heard appears in a box **before anything is
done with it**. They can correct a misheard word and then send it. Replies are
spoken aloud and written on screen, and they can stop the audio at any point.

When they have chosen a time, a confirmation card appears showing the
treatment, the dentist, the date and the time, with a five-minute countdown and
boxes for their name and phone number. **The appointment is only made when they
fill that in and press Confirm.** Nothing the assistant says books anything.

The phone number is always typed rather than spoken, even in a voice
conversation. Spoken digits are the single most common thing a computer
mishears, and a wrong number is a patient nobody can reach.

---

## Setting it up

There are two jobs. Someone sets up the practice once. Then each dentist
connects their own calendar, which takes about two minutes each and can only be
done by them.

### 1. The practice details

Before patients use it, we set up your working hours, your treatments and their
durations, and which dentists perform which treatments. Send us:

- Opening hours for each day of the week
- Each dentist's name, and whether they are senior
- Your treatments, how long each takes, and who performs each one
- The phone number patients should ring in an emergency
- The gap you want left between appointments, if not fifteen minutes

This is currently done by us rather than through a settings screen. Changing it
later is a short job, not a rebuild.

### 2. Each dentist connects their calendar

**This part happens outside the application, on Google's or Microsoft's own
site, and each dentist must do it with their own account.** Nobody can do it on
their behalf, which is the point: you are giving the assistant permission to
look at a specific person's diary, and only that person can grant it.

We will send each dentist a link. What follows is what they will see.

#### For a dentist using Google Calendar

1. The link opens Google's sign-in page. Sign in with the account that holds
   your working calendar. If you are signed in to more than one Google account,
   check carefully which one is selected.

2. **You may see a warning that the application has not been verified by
   Google.** This is expected while the practice is trialling the assistant.
   Google shows this for any application that has not yet been through its
   review process, which takes several weeks and is started once the practice
   decides to keep it. Choose **Advanced**, then **Go to (the practice's
   assistant)**.

3. Google asks for two permissions. Both are needed, and both are narrower than
   they might sound:

   - *See and edit events on all your calendars.* This is how confirmed
     appointments are added to your diary.
   - *View your availability.* This is how the assistant knows you are busy. It
     returns only whether a period is free or taken. **It does not say what the
     commitment is, who it is with, or what it is called.**

4. Approve, and you will land on a page confirming the calendar is connected.
   You can close the window.

#### For a dentist using Outlook

1. The link opens Microsoft's sign-in page. A work account and a personal
   Outlook account both work.

2. Microsoft asks for permission to read and write your calendar, and to stay
   connected. There is no warning screen for a personal account.

3. Approve, and the confirmation page appears as above.

Microsoft does not offer a permission as narrow as Google's "availability only"
one. The assistant still reads nothing but free and busy times, but on the
Microsoft side that is a property of our software rather than of the permission
you granted. It is worth knowing the difference.

#### If a dentist does not connect a calendar

Nothing breaks. The assistant still books them, still respects everything
already in the practice's own appointment book, and still keeps treatments to
the right people. The only thing it cannot see is their commitments elsewhere,
so those will need to stay off the assistant's diary another way.

Connecting is per dentist and can be done at any time.

### 3. While the assistant is on trial

Two things behave differently until the practice commits and the application
goes through Google's review.

**Only listed accounts can connect a Google calendar.** We add each dentist's
address to a list first. An address that is not on it will be refused.

**A Google connection lasts seven days.** After that the dentist re-opens the
link and approves again, which takes a few seconds. This is Google's rule for
applications still in trial, and it disappears once the review is complete.

Outlook connections do not expire this way.

---

## Running it day to day

**Appointments appear in the dentist's own calendar** as soon as a patient
confirms, under the treatment name and the patient's name. Cancel one in the
assistant and it disappears from the calendar too.

**Block time by putting it in your calendar.** Anything in a connected
dentist's calendar blocks that slot. There is nothing to learn: mark yourself
busy in the diary you already use, and the assistant will not offer the time.

**A held slot lasts five minutes.** If a patient walks away mid-booking, it
releases itself. Nobody has to tidy up.

### If something looks wrong

**The assistant says it cannot check availability.** A calendar service is
unreachable. It will refuse to offer times rather than guess, which is
deliberate, and it recovers by itself. If it persists, tell us.

**A dentist's calendar stopped being respected.** Their connection has expired
or been withdrawn. They open their link and approve again.

**A patient says a name or number is wrong.** They can tell the assistant in
the conversation and it will correct the record on their booking. They do not
need to cancel and rebook.

---

## Privacy, in plain terms

**What the assistant holds about a patient:** name, phone number, email
address, which treatment, when, and with whom. Nothing else. No clinical
notes, no history, no payment details.

**What it can see in a dentist's calendar:** whether a period is busy. Not the
title, not the attendees, not the notes.

**How a patient finds an existing appointment:** by giving the name and phone
number it was booked under. Both must match.

You should know the limit of that last one. It is the same check your
receptionist makes on the telephone, and it has the same weakness: somebody who
knows both could see when a patient is coming in, and could cancel it. We
recommend adding a code sent by text message before the assistant is opened to
the general public, and we have costed that work. Until it is in place, treat
the assistant as you would your phone line.

---

## What it costs to run

At a realistic booking volume for a three-dentist practice, the running cost is
roughly **55 to 110 US dollars a month**, most of it the artificial
intelligence itself and the hosted database. It scales with the number of
conversations rather than with the number of patients on your books, and it
costs nothing when nobody is using it.

A fuller breakdown, including what remains to be built before the assistant is
opened to the public, is in the internal document.
