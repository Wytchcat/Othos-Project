"""Who is speaking in a call: speaker labels from speech-to-text, turned into "you", a named guest, or "a guest".

Step 1 of voice separation (Nov 18, the user). Labels arrive with the transcript (LiveKit's user_input_transcribed
event, speaker_id) when the speech-to-text gives them: Deepgram's speaker labels (cloud, off unless turned on) or,
on the new build, a local voiceprint check. Without labels every call works as before: guest mode then goes by the
introduction and Othos's name only (agent.py).

Kept only for the call, in memory: no voice, voiceprint or label is written anywhere by this module.
"""
from collections import Counter


class Speakers:
    def __init__(self):
        self.owner: str | None = None        # the first voice in the call (a voiceprint will say it for sure)
        self.names: dict[str, str] = {}      # label -> guest name ("1" -> "Tina")
        self.guests: set[str] = set()        # labels of voices that aren't the owner's
        self._turn: list[str] = []
        self._expect: str | None = None      # a guest just introduced, whose voice hasn't been heard yet

    def heard(self, label: str | None, text: str) -> None:
        """A final piece of transcript and its speaker label (None when there is none)."""
        if label and text.strip():
            self._turn += [label] * max(1, len(text.split()))   # weighted by words

    def take(self) -> str | None:
        """The speaker of the turn just finished (most of its words), or None without labels. Starts the next turn."""
        if not self._turn:
            return None
        label = Counter(self._turn).most_common(1)[0][0]
        self._turn = []
        if self.owner is None:
            self.owner = label
        elif label != self.owner and self._expect and label not in self.names:
            self.names[label] = self._expect
            self.guests.add(label)
            self._expect = None
        return label

    def expect_guest(self, name: str) -> None:
        """The user just introduced someone: the next new voice is theirs (an unnamed one heard already, else the next)."""
        unnamed = [g for g in self.guests if g not in self.names]
        if unnamed:
            self.names[unnamed[-1]] = name
        else:
            self._expect = name

    def is_new_voice(self, label: str | None) -> bool:
        return bool(label) and label != self.owner and label not in self.guests

    def note_guest_voice(self, label: str) -> None:
        self.guests.add(label)

    def name(self, label: str | None) -> str | None:
        if not label:
            return None
        if label == self.owner:
            return "you"
        return self.names.get(label) or ("a guest" if label in self.guests else None)

    def label_for_model(self, label: str | None) -> str:
        """Who to tell the model said this turn: '' for the owner (or no labels), else the guest's name or 'a guest'."""
        n = self.name(label)
        return "" if n in (None, "you") else n
