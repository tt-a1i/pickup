"""Progress and coverage reporting for local session scans.

The report writes only to stderr by default; conversation output belongs on stdout.
"""
import ctypes
import ctypes.util
from functools import lru_cache
import io
from pathlib import Path
import re
import shutil
import sys
import time


# A literal run starting outside Python's repr escape alphabet, or a run of
# Han characters. Restrict anchors to characters with known lowercase rules.
_ANCHORS = re.compile(r'[ghijklmopqswyz][a-z]*|[\u3400-\u9fff]+')
_GENERATED_TEXT = '(initial) [image attachment: ] tool true false none nan inf'
_UNEXPECTED_NEWLINE = re.compile(rb'\n(?:(?<!}\n)(?<!}\r\n)|(?!\{|\Z))')


def _anchor(token):
    return max((match for match in _ANCHORS.findall(token) if match not in _GENERATED_TEXT),
               key=len, default='')


def _has_unicode_escape(data):
    offset = data.find(b'\\u')
    while offset >= 0:
        start = offset
        while start and data[start - 1] == 92:  # preceding backslash
            start -= 1
        if (offset - start) % 2 == 0:
            return True
        offset = data.find(b'\\u', offset + 2)
    return False


def jsonl_byte_lines(stream):
    # Match the text reader's support for CRLF and older CR line boundaries.
    for line in stream:
        if b'\r' in line:
            yield from line.splitlines()
        else:
            yield line


class QueryPrefilter:
    """Prove a negative using a mandatory literal in the raw JSONL.

    Each search token must occur somewhere, even for a match across fields.
    The decoder joins text with separators, can stringify tool values, and
    adds a few fixed labels. We only use substrings those operations cannot
    synthesize. Unicode escapes and ambiguous inputs defer to real decoding.
    """

    def __init__(self, query):
        tokens = query.split()
        self.required = max(map(_anchor, tokens), key=len, default='')
        self.anchor = self.required.encode('utf-8')
        self.ascii_anchor = self.anchor.isascii()
        # For multiple terms, any tool row can supply one word of a match.
        # Testing every tool row against every term costs more than decoding it.
        self.prune_tools = bool(self.anchor) and len(tokens) == 1

    def line_may_match(self, line):
        if self.anchor in line or _has_unicode_escape(line):
            return True
        if not self.ascii_anchor:
            return False
        # U+0130 and U+212A are the only non-ASCII characters whose lowercase
        # forms can supply a character in an ASCII letter anchor.
        return (b'i' in self.anchor and b'\xc4\xb0' in line
                or b'k' in self.anchor and b'\xe2\x84\xaa' in line
                or self.anchor in line.lower())

    def may_match(self, stream, ignored=None):
        # Search blocks in native byte operations. Finish a partial record so
        # neither anchors nor a run of escape backslashes can straddle blocks.
        while block := stream.read(256 * 1024):
            if not block.endswith(b'\n'):
                block += stream.readline()
            if (not block.startswith(b'{') or not block.endswith((b'}\n', b'}\r\n', b'}'))
                    or _UNEXPECTED_NEWLINE.search(block)):
                # Defer unexpected record boundaries to the decoder.
                return True
            if not self.line_may_match(block):
                continue
            if ignored is None or any(not ignored.match(line) and self.line_may_match(line)
                                      for line in jsonl_byte_lines(io.BytesIO(block))):
                return True
        return False


class _ZstdBuffer(ctypes.Structure):
    _fields_ = [('data', ctypes.c_void_p), ('size', ctypes.c_size_t), ('pos', ctypes.c_size_t)]


@lru_cache(maxsize=1)
def zstd_library():
    """Find the system library, including Homebrew beside the zstd executable."""
    command = shutil.which('zstd')
    sibling = Path(command).resolve().parent.parent / 'lib' / 'libzstd.dylib' if command else None
    for name in (sibling, ctypes.util.find_library('zstd')):
        if not name:
            continue
        try:
            lib = ctypes.CDLL(str(name))
            lib.ZSTD_createDStream.argtypes = []
            lib.ZSTD_createDStream.restype = ctypes.c_void_p
            lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
            lib.ZSTD_freeDStream.restype = ctypes.c_size_t
            lib.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ZstdBuffer),
                                                  ctypes.POINTER(_ZstdBuffer)]
            lib.ZSTD_decompressStream.restype = ctypes.c_size_t
            lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
            lib.ZSTD_isError.restype = ctypes.c_uint
            return lib
        except (OSError, AttributeError):
            continue
    return None


class ZstdReader(io.RawIOBase):
    """Stream frames with libzstd on Python versions without compression.zstd."""

    def __init__(self, source, lib):
        self._source = source
        self._lib = lib
        self._context = lib.ZSTD_createDStream()
        if not self._context:
            raise OSError('Cannot allocate Zstandard decoder')
        self._chunk = b''
        self._input = _ZstdBuffer()
        self._remaining = 1  # A complete frame must be seen before EOF.

    def readable(self):
        return True

    def readinto(self, buffer):
        if not buffer:
            return 0
        target = (ctypes.c_char * len(buffer)).from_buffer(buffer)
        output = _ZstdBuffer(ctypes.addressof(target), len(buffer), 0)
        while not output.pos:
            if self._input.pos == self._input.size:
                self._chunk = self._source.read(128 * 1024)
                self._input = _ZstdBuffer(ctypes.cast(self._chunk, ctypes.c_void_p).value, len(self._chunk), 0)
                if not self._chunk and not self._remaining:
                    return 0
            self._remaining = self._lib.ZSTD_decompressStream(self._context, output, self._input)
            if self._lib.ZSTD_isError(self._remaining):
                raise OSError('Invalid Zstandard history')
            if not self._chunk and not output.pos and self._remaining:
                raise OSError('Truncated Zstandard history')
        return output.pos

    def close(self):
        if self._context:
            self._lib.ZSTD_freeDStream(self._context)
            self._context = None
        super().close()


class ScanReport:
    def __init__(self, agents, stream=None, clock=None):
        self.stream = stream if stream is not None else sys.stderr
        self.clock = clock if clock is not None else time.monotonic
        self.stats = {
            agent: {'available': None, 'discovered': 0, 'excluded': 0,
                    'attempted': 0, 'readable': 0, 'metadata_only': 0, 'failed': 0,
                    'indexed_text': 0, 'sqlite_history': 0, 'prefiltered': 0,
                    'discovery_failed': 0, 'warnings': 0}
            for agent in agents
        }
        self._last_progress = None
        self._started_agents = set()
        self._write('Scan started: ' + ', '.join(self.stats))

    def _write(self, message):
        print(message, file=self.stream, flush=True)

    @staticmethod
    def _line(value):
        return ' '.join(str(value).split())

    def source(self, agent, available):
        self.stats[agent]['available'] = bool(available)

    def discovered(self, agent, count):
        self.stats[agent]['discovered'] = count

    def excluded(self, agent):
        self.stats[agent]['excluded'] += 1

    def attempted(self, agent, uid):
        self.stats[agent]['attempted'] += 1
        now = self.clock()
        if agent not in self._started_agents or self._last_progress is None or now - self._last_progress >= 1:
            self._write(f'Scanning {agent}: {self.stats[agent]["attempted"]} attempted (current {self._line(uid)})')
            self._last_progress = now
        self._started_agents.add(agent)

    def readable(self, agent, fidelity=''):
        self.stats[agent]['readable'] += 1
        if fidelity:
            self.stats[agent][fidelity] += 1

    def prefiltered(self, agent):
        self.stats[agent]['prefiltered'] += 1

    def metadata_only(self, agent):
        self.stats[agent]['metadata_only'] += 1

    def failed(self, agent, uid, reason):
        self.stats[agent]['failed'] += 1
        self._write(f'Scan failed [{agent} {self._line(uid)}]: {self._line(reason)}')

    def discovery_failed(self, agent, reason):
        self.stats[agent]['discovery_failed'] += 1
        self._write(f'Discovery failed [{agent}]: {self._line(reason)}')

    def warning(self, agent, reason):
        self.stats[agent]['warnings'] += 1
        self._write(f'Scan warning [{agent}]: {self._line(reason)}')

    def coverage_label(self, status):
        if any(stats['prefiltered'] for stats in self.stats.values()):
            return f'{status} (raw prefilter skipped JSON validation for some files)'
        return status

    def finish(self):
        totals = dict.fromkeys(('discovered', 'excluded', 'attempted', 'readable', 'metadata_only',
                                'indexed_text', 'sqlite_history', 'prefiltered',
                                'failed', 'unscanned', 'discovery_failed'), 0)
        for stats in self.stats.values():
            stats['unscanned'] = max(0, stats['discovered'] - stats['excluded'] - stats['attempted'])
            for key in totals:
                totals[key] += stats[key]
        partial = bool(totals['failed'] or totals['discovery_failed'] or totals['metadata_only'] or totals['unscanned']
                       or totals['indexed_text'] or totals['sqlite_history']
                       or any(stats['warnings'] for stats in self.stats.values()))
        status = 'partial' if partial else 'complete'
        self._write(f'Scan coverage: {self.coverage_label(status)}; '
                    + ', '.join(f'{key}={value}' for key, value in totals.items()))
        for agent, stats in self.stats.items():
            availability = 'absent' if stats['available'] is False else ('present' if stats['available'] else 'unknown')
            counts = ', '.join(f'{key}={stats[key]}' for key in totals)
            self._write(f'  {agent}: {availability}; {counts}; warnings={stats["warnings"]}')
        return status
