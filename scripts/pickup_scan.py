"""Progress and coverage reporting for local session scans.

The report writes only to stderr by default; conversation output belongs on stdout.
"""
import sys
import time


class ScanReport:
    def __init__(self, agents, stream=None, clock=None):
        self.stream = stream if stream is not None else sys.stderr
        self.clock = clock if clock is not None else time.monotonic
        self.stats = {
            agent: {'available': None, 'discovered': 0, 'excluded': 0,
                    'attempted': 0, 'readable': 0, 'failed': 0,
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

    def readable(self, agent):
        self.stats[agent]['readable'] += 1

    def failed(self, agent, uid, reason):
        self.stats[agent]['failed'] += 1
        self._write(f'Scan failed [{agent} {self._line(uid)}]: {self._line(reason)}')

    def discovery_failed(self, agent, reason):
        self.stats[agent]['discovery_failed'] += 1
        self._write(f'Discovery failed [{agent}]: {self._line(reason)}')

    def warning(self, agent, reason):
        self.stats[agent]['warnings'] += 1
        self._write(f'Scan warning [{agent}]: {self._line(reason)}')

    def finish(self):
        totals = dict.fromkeys(('discovered', 'excluded', 'attempted', 'readable', 'failed', 'unscanned', 'discovery_failed'), 0)
        for stats in self.stats.values():
            stats['unscanned'] = max(0, stats['discovered'] - stats['excluded'] - stats['attempted'])
            for key in totals:
                totals[key] += stats[key]
        partial = bool(totals['failed'] or totals['discovery_failed'] or totals['unscanned']
                       or any(stats['warnings'] for stats in self.stats.values()))
        status = 'partial' if partial else 'complete'
        self._write(f'Scan coverage: {status}; ' + ', '.join(f'{key}={value}' for key, value in totals.items()))
        for agent, stats in self.stats.items():
            availability = 'absent' if stats['available'] is False else ('present' if stats['available'] else 'unknown')
            counts = ', '.join(f'{key}={stats[key]}' for key in totals)
            self._write(f'  {agent}: {availability}; {counts}; warnings={stats["warnings"]}')
        return status
