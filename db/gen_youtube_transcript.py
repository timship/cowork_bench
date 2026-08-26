"""Regenerate the youtube.transcripts fixture row for video 7ZQzGq32kAY.

Usage: python3 db/gen_youtube_transcript.py > row.tsv
Then replace line 463003 of the uncompressed db/init.sql with it.


The original row was a verbatim YouTube ASR caption track of a copyrighted
afrobeat mix — song lyrics, ~40KB, shipped in db/init.sql.gz. Both tasks that
consume it grade structurally (heading counts, >=8 playlist rows, >=1 artist
from a fixed list), so a host-narrated mix transcript serves the task at least
as well while containing no third-party lyrics: segment titles are mix segments,
and artist names are plain facts about who is in the genre.
"""
import json
import random

# Fixed seed: the fixture must be byte-identical on every regeneration.
random.seed(20241029)

SEGMENTS = [
    ("Lagos Nights Groove",   "Davido",      "warm opener, tell the crowd the night is young"),
    ("Sunset Highlife",       "Burna Boy",   "let the horns breathe before you talk over it"),
    ("Accra Bounce",          "Wizkid",      "call out the drum pattern, invite hands in the air"),
    ("Palm Wine Interlude",   "Tems",        "keep it soft, this is the mid-set breather"),
    ("Detty December",        "Rema",        "biggest singalong of the hour, push the chorus"),
    ("Harmattan Drive",       "Asake",       "pick the tempo back up, quick talkover only"),
    ("Marina Sunrise",        "Ayra Starr",  "dedicate this one to the early callers"),
    ("Kokoma Shuffle",        "CKay",        "shout out the dancers, name the step"),
    ("Zanku Reload",          "Kizz Daniel", "drop the tag before the beat switch"),
    ("Coastal Cooldown",      "Omah Lay",    "bring the room down gently, tease the finale"),
    ("Afro Fusion Finale",    "Fireboy",     "biggest energy, count the crowd in"),
    ("Closing Bed",           "Burna Boy",   "read the station ident over the outro"),
]

# Varied per-segment filler so the twelve segments are distinguishable in the
# transcript the way a real host-narrated mix would be, and so the timeline
# length stays close to the original (~1385 s).
LINES_PER_SEGMENT = [
    "you are locked into the afrobeat sunday show coming to you all night long",
    "this next stretch of the mix is called {title} and it runs on a steady afrobeat groove",
    "that is {artist} carrying the segment and you can hear the drums sit right behind the bass",
    "[Music]",
    "keep it right here because the rhythm does not let up through this part of the set",
    "[Music]",
    "the percussion in this section is where the whole segment lives",
    "[Music]",
    "if you are just tuning in this is {title} and we are about halfway through it",
    "[Music]",
    "listen to how the bassline walks underneath the vocal line here",
    "we are riding this groove for a few more bars before the switch",
    "[Music]",
    "[Applause]",
    "hands up across the room for {artist} right now",
    "[Music]",
]

snips, content_parts, t = [], [], 1.35

def add(text, dur):
    global t
    snips.append({"text": text, "start": round(t, 2), "duration": round(dur, 2)})
    content_parts.append(text)
    t += dur

add("[Music]", 7.5)
add("good evening lagos and good evening to everybody streaming in tonight", 5.4)
add("this is the afrobeat mix twenty twenty four and we have twelve segments ahead of us", 6.2)

for i, (title, artist, _note) in enumerate(SEGMENTS, 1):
    add(f"segment number {i} coming up now", 4.1)
    # Segments run different lengths, the way tracks in a real mix do — otherwise
    # "compute the duration of each segment" collapses to one repeated number.
    body = LINES_PER_SEGMENT[:]
    extra = random.randint(0, 6)
    for _ in range(extra):
        body.insert(random.randrange(3, len(body)), "[Music]")
    for line in body:
        dur = random.uniform(6.0, 9.5) if line.startswith("[") else random.uniform(4.6, 7.2)
        add(line.format(title=title, artist=artist), dur)
    add(f"that was {title} with {artist} and we are moving straight on", 5.5)

add("that is the full mix for tonight thank you for riding with us", 5.6)
add("[Music]", 8.0)

content = " ".join(content_parts)
row = "\t".join(["7ZQzGq32kAY", "en", "\\N", content,
                 json.dumps(snips, ensure_ascii=False)])

import sys
sys.stdout.write(row + "\n")
print(f"segments   : {len(SEGMENTS)}", file=sys.stderr)
print(f"snippets   : {len(snips)}", file=sys.stderr)
print(f"timeline   : {t:.0f}s", file=sys.stderr)
print(f"content    : {len(content)} bytes", file=sys.stderr)
print(f"row        : {len(row)} bytes", file=sys.stderr)
print(f"artists    : {len({a for _,a,_ in SEGMENTS})}", file=sys.stderr)
