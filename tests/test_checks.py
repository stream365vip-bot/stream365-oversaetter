import pytest

from oversaetter.checks import EnglishIndex, check_all, check_cue, known_bad_words
from oversaetter.config import Config
from tests.helpers import cue


def flags(da, en="", max_chars=42):
    return check_cue(da, en, max_chars)


def test_good_line_has_no_flags():
    assert flags("Jeg kommer i morgen.", "I'll come tomorrow.") == []


@pytest.mark.parametrize(
    "da",
    [
        "Wait, jeg skal lige tjekke.",
        "Hmm, lad mig se.",
        "Let's see: Jeg elsker dig.",
        "Translation: Hvor er du?",
        "Final output: Hvor er du?",
        "Line 1: Hvor er du?",
        "Hvor er du? (16 chars)",
        "Ifølge the source betyder det ja.",
    ],
)
def test_english_notes(da):
    assert "english_note" in flags(da, "Where are you?")


def test_note_word_also_in_english_is_ok():
    assert "english_note" not in flags("Hmm?", "Hmm?")
    assert "english_note" not in flags("Det er den final, vi venter på.", "It's the final we're waiting for.")


def test_whole_english_sentence():
    assert "english_sentence" in flags("I don't know what you're talking about.", "Jeg ved ikke...")
    assert "english_sentence" not in flags("Okay, cool. Vi ses.", "Okay, cool. See you.")


def test_too_many_lines_and_too_long():
    assert "too_many_lines" in flags("En\nTo\nTre", "One\nTwo\nThree")
    assert "line_too_long" in flags("Det her er en alt for lang linje til en undertekst", "x")
    # kursiv-tags tæller ikke med i længden
    assert "line_too_long" not in flags("<i>" + "a" * 42 + "</i>", "x")


@pytest.mark.parametrize("da", ["Hvad gör du?", "Det är fint.", "Über alles."])
def test_foreign_letters(da):
    assert "foreign_letters" in flags(da)


@pytest.mark.parametrize(
    "da", ["Uansett hvad.", "Han leter efter dig.", "Det er lissom svært.", "Jeg ved ikkje.", "Bara lidt."]
)
def test_norwegian_swedish_words(da):
    assert "nordic_word" in flags(da)


def test_danish_words_are_not_nordic():
    assert "nordic_word" not in flags("Jeg ved ikke, hvorfor han bare går.")


@pytest.mark.parametrize("da", ["Kom here.", "Jeg kommer, so snart jeg kan.", "Vent llige.", "Han will komme."])
def test_known_misspellings(da):
    assert "english_misspelling" in flags(da)


def test_forskellige_is_not_misspelling():
    assert "english_misspelling" not in flags("De er forskellige.")


def test_missing_number():
    assert "missing_number" in flags("Vi ses klokken fem.", "See you at 5.")
    assert "missing_number" not in flags("Vi ses klokken 5.", "See you at 5.")
    assert "missing_number" not in flags("Det koster 1.000 kr.", "It costs 1,000.")
    assert "missing_number" not in flags("Klokken 3.30.", "At 3:30.")


def test_empty_line():
    assert flags("", "Hello") == ["empty"]
    assert flags("<i></i>", "Hello") == ["empty"]


def test_identical_to_english():
    assert "identical_to_english" in flags("Where are you going?", "Where are you going?")
    assert "identical_to_english" in flags("Hello.", "Hello.")
    # navne og udråb er tilladt
    assert "identical_to_english" not in flags("Michael!", "Michael!")
    assert "identical_to_english" not in flags("Okay.", "Okay.")
    assert "identical_to_english" not in flags("Wow!", "Wow!")


def test_match_on_timecodes():
    en = [cue(1, 2, "One"), cue(3, 4, "I have 2 dogs."), cue(5, 6, "Three")]
    da = [cue(1, 2, "En"), cue(3.05, 4, "Jeg har to hunde."), cue(5, 6, "Tre")]
    result = check_all(da, EnglishIndex(en), 42)
    assert result == {1: ["missing_number"]}


def test_match_on_overlap_when_times_differ():
    en = [cue(10, 12, "We need 3 cars.")]
    da = [cue(10.5, 12.2, "Vi skal bruge 3 biler.")]
    assert EnglishIndex(en).text_for(da[0]) == "We need 3 cars."
    assert check_all(da, EnglishIndex(en), 42) == {}


# --- kendte forkerte ord -------------------------------------------------------------
@pytest.mark.parametrize(
    "da",
    [
        "Han er en bestie.",
        "Bestien kommer!",
        "Menneskebestien er løs.",
        "Hvad er det for en MENNESKEBESTIE?",
        "Uansett hvad.",
        "Kom nu, please.",
        "Tak, sweetheart.",
        "Det var bare en prank.",
        "<i>Vent llige.</i>",
        "Kom here.",
    ],
)
def test_known_bad_word_found(da):
    assert "known_bad_word" in flags(da)


@pytest.mark.parametrize(
    "da",
    [
        "Jeg har lavet en bestilling.",
        "Han er bestyrer af den bedste.",
        "Pleasen var lige ved.",
        "Uhyret kommer.",
        "Hvor er menneskeuhyret?",
        "Jeg kommer i morgen.",
    ],
)
def test_known_bad_word_not_found(da):
    assert "known_bad_word" not in flags(da)


@pytest.mark.parametrize("word", ["okay", "hey", "wow", "yo", "cool", "nice", "shit", "fuck", "bro"])
def test_allowed_loanwords_are_never_bad_words(word):
    da = f"{word.capitalize()}, det er {word}."
    assert "known_bad_word" not in flags(da)
    # heller ikke hvis nogen skriver dem i KNOWN_BAD_WORDS
    words = Config.from_env({"KNOWN_BAD_WORDS": f"{word}, bestie"}).bad_words()
    assert word not in words and "bestie" in words
    assert "known_bad_word" not in check_cue(da, "", 42, words)


def test_extra_bad_words_from_env():
    words = Config.from_env({"KNOWN_BAD_WORDS": " Darling ,honey,, "}).bad_words()
    assert words == known_bad_words() | {"darling", "honey"}
    assert "known_bad_word" in check_cue("Hej, darling.", "", 42, words)
    assert "known_bad_word" in check_cue("HONEY!", "", 42, words)
    assert "known_bad_word" not in check_cue("Hej, darling.", "", 42)  # ikke i startlisten
    assert "known_bad_word" not in check_cue("Honeymoon.", "", 42, words)  # del af andet ord
    # startlisten gælder stadig
    assert "known_bad_word" in check_cue("Kom nu, please.", "", 42, words)


def test_check_all_uses_bad_words():
    da = [cue(1, 2, "Hej, darling."), cue(3, 4, "Hej, please.")]
    words = known_bad_words("darling")
    assert check_all(da, EnglishIndex([]), 42, words) == {0: ["known_bad_word"], 1: ["known_bad_word"]}
    assert check_all(da, EnglishIndex([]), 42) == {1: ["known_bad_word"]}
