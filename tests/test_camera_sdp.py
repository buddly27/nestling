"""Unit tests for SDP munge functions.

These are pure string transforms with no I/O — the most valuable tests in the
project because they guard the exact SDM quirks that caused problems in the
prior Go implementation.
"""

import textwrap

import pytest

from nestling.camera import add_fake_ssrc, fix_bundle_order, fix_candidates, fix_video_codec

_VIDEO_SSRC = "1933910976"
_RTX_SSRC = "504479091"
_CNAME = "DFsz7BwXusRJ+YWb"


def _sdp(*sections: str) -> str:
    """Build a minimal SDP string with \r\n line endings."""
    return "\r\n".join(line for section in sections for line in section.strip().splitlines())


_HEADER = """
v=0
o=- 1 1 IN IP4 0.0.0.0
s=-
t=0 0
a=group:BUNDLE 0 1 2
"""

_AUDIO_SECTION = """
m=audio 9 UDP/TLS/RTP/SAVPF 111
a=recvonly
a=ssrc:12345 cname:audio-cname
"""

_VIDEO_SECTION_BARE = """
m=video 9 UDP/TLS/RTP/SAVPF 96 97
a=sendrecv
"""

_VIDEO_SECTION_PT0 = """
m=video 9 UDP/TLS/RTP/SAVPF 0
a=sendrecv
"""

_VIDEO_SECTION_WITH_SSRC = """
m=video 9 UDP/TLS/RTP/SAVPF 96 97
a=sendrecv
a=ssrc:9999 cname:random
a=ssrc:8888 cname:random
"""


# ---------------------------------------------------------------------------
# add_fake_ssrc
# ---------------------------------------------------------------------------

class TestAddFakeSSRC:
    def test_injects_ssrc_block_when_none_present(self):
        sdp = _sdp(_HEADER, _VIDEO_SECTION_BARE)
        result = add_fake_ssrc(sdp)
        assert f"a=ssrc:{_VIDEO_SSRC} cname:{_CNAME}" in result
        assert f"a=ssrc:{_RTX_SSRC} cname:{_CNAME}" in result
        assert f"a=ssrc-group:FID {_VIDEO_SSRC} {_RTX_SSRC}" in result

    def test_replaces_existing_random_ssrc_lines(self):
        sdp = _sdp(_HEADER, _VIDEO_SECTION_WITH_SSRC)
        result = add_fake_ssrc(sdp)
        assert "9999" not in result
        assert "8888" not in result
        assert f"a=ssrc:{_VIDEO_SSRC}" in result
        assert f"a=ssrc:{_RTX_SSRC}" in result

    def test_ssrc_block_not_duplicated(self):
        sdp = _sdp(_HEADER, _VIDEO_SECTION_BARE)
        result = add_fake_ssrc(sdp)
        assert result.count(f"a=ssrc:{_VIDEO_SSRC}") == 1

    def test_audio_ssrc_lines_untouched(self):
        sdp = _sdp(_HEADER, _AUDIO_SECTION, _VIDEO_SECTION_BARE)
        result = add_fake_ssrc(sdp)
        assert "a=ssrc:12345 cname:audio-cname" in result

    def test_injected_ssrc_in_video_section_not_audio(self):
        sdp = _sdp(_HEADER, _AUDIO_SECTION, _VIDEO_SECTION_BARE)
        result = add_fake_ssrc(sdp)
        lines = result.replace("\r\n", "\n").splitlines()
        in_audio = False
        for line in lines:
            if line.startswith("m="):
                in_audio = line.startswith("m=audio")
            if in_audio and _VIDEO_SSRC in line:
                pytest.fail(f"Fake video SSRC leaked into audio section: {line!r}")

    def test_idempotent_on_second_call(self):
        sdp = _sdp(_HEADER, _VIDEO_SECTION_BARE)
        once  = add_fake_ssrc(sdp)
        twice = add_fake_ssrc(once)
        assert once.count(f"a=ssrc:{_VIDEO_SSRC}") == twice.count(f"a=ssrc:{_VIDEO_SSRC}")


# ---------------------------------------------------------------------------
# fix_video_codec
# ---------------------------------------------------------------------------

class TestFixVideoCodec:
    def test_rewrites_pt0_to_pt96(self):
        sdp = _sdp(_HEADER, _VIDEO_SECTION_PT0)
        result = fix_video_codec(sdp)
        assert "m=video 9 UDP/TLS/RTP/SAVPF 96" in result
        assert "SAVPF 0" not in result

    def test_injects_rtpmap_when_pt0_rewritten(self):
        # Only injects a=rtpmap:96 when PT=0 was actually rewritten to PT=96.
        sdp = _sdp(_HEADER, _VIDEO_SECTION_PT0)
        result = fix_video_codec(sdp)
        assert "a=rtpmap:96 VP8/90000" in result

    def test_no_rtpmap_injection_when_pt_already_correct(self):
        # PT=96 already present and no rewrite happened — do not inject spurious rtpmap.
        sdp = _sdp(_HEADER, _VIDEO_SECTION_BARE)
        result = fix_video_codec(sdp)
        assert "a=rtpmap:96 VP8/90000" not in result

    def test_does_not_duplicate_existing_rtpmap(self):
        video = """
m=video 9 UDP/TLS/RTP/SAVPF 96
a=rtpmap:96 VP8/90000
a=sendrecv
"""
        sdp = _sdp(_HEADER, video)
        result = fix_video_codec(sdp)
        assert result.count("a=rtpmap:96 VP8/90000") == 1

    def test_does_not_touch_audio_pt(self):
        audio_pt0 = """
m=audio 9 UDP/TLS/RTP/SAVPF 0
a=recvonly
"""
        sdp = _sdp(_HEADER, audio_pt0, _VIDEO_SECTION_BARE)
        result = fix_video_codec(sdp)
        assert "m=audio 9 UDP/TLS/RTP/SAVPF 0" in result  # audio PT=0 is untouched

    def test_noop_when_already_correct(self):
        video = """
m=video 9 UDP/TLS/RTP/SAVPF 96
a=rtpmap:96 VP8/90000
a=sendrecv
"""
        sdp = _sdp(_HEADER, video)
        result = fix_video_codec(sdp)
        assert result == fix_video_codec(result)  # idempotent


# ---------------------------------------------------------------------------
# fix_bundle_order
# ---------------------------------------------------------------------------

class TestFixBundleOrder:
    def test_rewrites_sdm_bundle_order(self):
        sdp = "v=0\r\na=group:BUNDLE 0 2 1\r\nm=audio 9"
        result = fix_bundle_order(sdp)
        assert "a=group:BUNDLE 0 1 2" in result
        assert "a=group:BUNDLE 0 2 1" not in result

    def test_noop_when_already_correct(self):
        sdp = "v=0\r\na=group:BUNDLE 0 1 2\r\nm=audio 9"
        result = fix_bundle_order(sdp)
        assert "a=group:BUNDLE 0 1 2" in result

    def test_only_fixes_first_occurrence(self):
        sdp = "a=group:BUNDLE 0 2 1\r\na=group:BUNDLE 0 2 1"
        result = fix_bundle_order(sdp)
        lines = [l for l in result.replace("\r\n", "\n").splitlines() if "BUNDLE" in l]
        assert lines[0] == "a=group:BUNDLE 0 1 2"
        assert lines[1] == "a=group:BUNDLE 0 2 1"  # second occurrence untouched

    def test_tolerates_missing_bundle_line(self):
        sdp = "v=0\r\nm=audio 9\r\nm=video 9"
        result = fix_bundle_order(sdp)
        assert result == sdp  # unchanged

    def test_crlf_preserved(self):
        sdp = "v=0\r\na=group:BUNDLE 0 2 1\r\nm=audio 9"
        result = fix_bundle_order(sdp)
        assert "\r\n" in result


# ---------------------------------------------------------------------------
# fix_candidates
# ---------------------------------------------------------------------------

class TestFixCandidates:
    def test_inserts_foundation_when_missing(self):
        # SDM sends "a=candidate: 1 udp ..." — space after colon, no foundation
        sdp = "v=0\r\na=candidate: 1 udp 2122262655 192.168.1.1 56789 typ host\r\n"
        result = fix_candidates(sdp)
        assert "a=candidate:sdm 1 udp 2122262655 192.168.1.1 56789 typ host" in result

    def test_leaves_well_formed_candidate_untouched(self):
        sdp = "v=0\r\na=candidate:abc123 1 udp 2122262655 192.168.1.1 56789 typ host\r\n"
        result = fix_candidates(sdp)
        assert "a=candidate:abc123 1 udp" in result
        assert "sdm" not in result

    def test_multiple_malformed_candidates_all_fixed(self):
        sdp = (
            "a=candidate: 1 udp 2122262655 10.0.0.1 5000 typ host\r\n"
            "a=candidate: 1 udp 1685987071 203.0.113.1 5000 typ srflx\r\n"
        )
        result = fix_candidates(sdp)
        assert result.count("a=candidate:sdm") == 2

    def test_non_candidate_lines_untouched(self):
        sdp = "v=0\r\na=ice-ufrag:xyz\r\na=candidate: 1 udp 100 1.2.3.4 5678 typ host\r\n"
        result = fix_candidates(sdp)
        assert "a=ice-ufrag:xyz" in result
        assert "a=candidate:sdm 1 udp" in result

    def test_idempotent(self):
        sdp = "a=candidate: 1 udp 100 1.2.3.4 5678 typ host\r\n"
        once = fix_candidates(sdp)
        twice = fix_candidates(once)
        assert once == twice
