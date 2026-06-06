import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import transcript  # noqa: E402


VTT = """\
WEBVTT

NOTE
This file was auto-generated.

00:00:01.000 --> 00:00:04.000
<v Alice>Welcome everyone to the monthly call.</v>

00:00:04.000 --> 00:00:08.000 align:start position:0%
We shipped the storage account module this sprint.
"""

SRT = """\
1
00:00:01,000 --> 00:00:04,000
Welcome everyone to the monthly call.

2
00:00:04,000 --> 00:00:08,000
We shipped the storage account module this sprint.
"""

# Rolling auto-captions: the same line repeats across overlapping cues.
VTT_DUPES = """\
WEBVTT

00:00:01.000 --> 00:00:03.000
the next item is the roadmap

00:00:03.000 --> 00:00:05.000
the next item is the roadmap

00:00:05.000 --> 00:00:07.000
we want feedback on it
"""


class TestNormalizeTranscript(unittest.TestCase):
    def test_vtt_stripped_to_prose(self):
        out = transcript.normalize_transcript(VTT)
        self.assertEqual(
            out,
            "Welcome everyone to the monthly call.\n"
            "We shipped the storage account module this sprint.")
        # no structure leaks through
        self.assertNotIn("-->", out)
        self.assertNotIn("WEBVTT", out)
        self.assertNotIn("NOTE", out)
        self.assertNotIn("<v", out)

    def test_srt_stripped_to_prose(self):
        out = transcript.normalize_transcript(SRT)
        self.assertEqual(
            out,
            "Welcome everyone to the monthly call.\n"
            "We shipped the storage account module this sprint.")
        self.assertNotIn("-->", out)
        # the standalone SRT cue-index digits are gone
        self.assertNotIn("\n1\n", "\n" + out + "\n")

    def test_consecutive_duplicate_captions_collapse(self):
        out = transcript.normalize_transcript(VTT_DUPES)
        self.assertEqual(
            out,
            "the next item is the roadmap\n"
            "we want feedback on it")

    def test_plain_text_passes_through(self):
        plain = "Topic one: releases.\n\nTopic two: roadmap.\n"
        self.assertEqual(
            transcript.normalize_transcript(plain),
            "Topic one: releases.\n\nTopic two: roadmap.")

    def test_markdown_passes_through_trimmed(self):
        md = "# Call notes   \n\n- decided to ship v2   \n"
        self.assertEqual(
            transcript.normalize_transcript(md),
            "# Call notes\n\n- decided to ship v2")

    def test_empty_and_whitespace(self):
        self.assertEqual(transcript.normalize_transcript(""), "")
        self.assertEqual(transcript.normalize_transcript("   \n\n  "), "")

    def test_bom_is_stripped(self):
        self.assertEqual(
            transcript.normalize_transcript("\ufeffWEBVTT\n\n"
                                            "00:00:01.000 --> 00:00:02.000\nhi"),
            "hi")

    def test_vtt_header_metadata_block_stripped(self):
        # WEBVTT header metadata (Kind:, Language:) runs until the first blank line
        # and must NOT leak into the prose.
        vtt = ("WEBVTT\nKind: captions\nLanguage: en\n\n"
               "00:00:00.480 --> 00:00:03.120\nwelcome to the call")
        out = transcript.normalize_transcript(vtt)
        self.assertEqual(out, "welcome to the call")
        self.assertNotIn("Kind:", out)
        self.assertNotIn("Language:", out)

    def test_inline_timestamp_tags_removed(self):
        vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n"
               "we <00:00:01.500>shipped<00:00:01.900> it")
        self.assertEqual(transcript.normalize_transcript(vtt), "we shipped it")


class TestTranscriptCli(unittest.TestCase):
    def test_cli_prints_normalized(self):
        import io
        import contextlib
        fd, path = tempfile.mkstemp(suffix=".vtt")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(VTT)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            transcript.main([path])
        self.assertIn("Welcome everyone to the monthly call.", buf.getvalue())
        self.assertNotIn("-->", buf.getvalue())

    def test_cli_missing_file_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            transcript.main(["/no/such/transcript.vtt"])
        self.assertEqual(cm.exception.code, 2)

    def test_cli_bad_args_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            transcript.main([])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
