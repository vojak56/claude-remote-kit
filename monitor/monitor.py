"""Live dashboard of every Claude Code session on this machine.

Run:  python monitor/monitor.py      (scripts/start-monitor.ps1 opens it in a WezTerm tab)
It only reads; it never writes anywhere.

Data sources:
  ~/.claude/sessions/<pid>.json          registry of running sessions: busy/idle, folder, id
  ~/.claude/projects/<folder>/<id>.jsonl session transcript: prompts, tool calls, answers, tokens
  process tree                           which terminal a session lives in (WezTerm window, remote env)

The screen answers one question: where is work happening, and where is a session waiting for me.
  > working     big card with the steps of the current turn
  v your turn   the session answered and waits for you
  o quiet       nothing for a while;  . fresh - no prompts yet;  x down - a remote environment died
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import zlib
from collections import deque
from datetime import datetime
from pathlib import Path

import psutil
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

sys.path.insert(0, str(Path(__file__).parent))
from kitconfig import (CONTEXT_TOKENS, PROJECTS_FILE, SERVICES, STATE_DIR,  # noqa: E402
                       WEZTERM, YOUR_TURN_MINUTES)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

CLAUDE_HOME = Path.home() / '.claude'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# --- palette (AMOLED black + Nord) ----------------------------------------------
HEADER_BG = '#111111'
ACCENT = '#e6e141'
GREEN = '#a3be8c'
RED = '#bf616a'
ORANGE = '#d08770'
TEXT = '#d8dee9'
BRIGHT = '#eceff4'
DIM = '#6b7280'
DARKER = '#3b4252'
BORDER = '#262b33'
SESSION_COLORS = ['#88c0d0', '#b48ead', '#d08770', '#8fbcbb', '#81a1c1', '#ebcb8b', '#a3be8c', '#e5a8c8']

SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
BARS = '▁▂▃▄▅▆▇█'
CHART_MINUTES = 30
SIDEBAR_WIDTH = 58

TOOLS = {
    'Bash': ('SHELL', '#81a1c1'), 'PowerShell': ('SHELL', '#81a1c1'),
    'Read': ('READ', '#8fbcbb'), 'Grep': ('FIND', '#8fbcbb'), 'Glob': ('FIND', '#8fbcbb'),
    'Edit': ('EDIT', GREEN), 'Write': ('WRITE', GREEN), 'NotebookEdit': ('EDIT', GREEN),
    'Agent': ('AGENT', '#b48ead'), 'Workflow': ('FLOW', '#b48ead'),
    'WebSearch': ('WEB', '#88c0d0'), 'WebFetch': ('WEB', '#88c0d0'),
    'Skill': ('SKILL', '#ebcb8b'), 'Artifact': ('PAGE', ORANGE), 'AskUserQuestion': ('ASK', ORANGE),
}

# session states, in display order
ATTENTION, WORKING, YOUR_TURN, QUIET, FRESH = 'attention', 'working', 'your turn', 'quiet', 'fresh'
ORDER = {ATTENTION: 0, WORKING: 1, YOUR_TURN: 2, QUIET: 3, FRESH: 4}


def now():
    return time.time()


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def ago(sec):
    sec = max(0, int(sec))
    if sec < 60:
        return f'{sec}s'
    if sec < 3600:
        return f'{sec // 60} min'
    if sec < 86400:
        return f'{sec // 3600}h {sec % 3600 // 60:02d}m'
    return f'{sec // 86400}d'


def clock(sec):
    sec = max(0, int(sec))
    return f'{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}' if sec >= 3600 else f'{sec // 60:02d}:{sec % 60:02d}'


def duration(sec):
    if sec < 1:
        return f'{sec:.1f}s'
    if sec < 60:
        return f'{int(sec)}s'
    return clock(sec)


def short(s, n):
    s = ' '.join(str(s).split())
    n = max(1, n)
    return s if len(s) <= n else s[: n - 1] + '…'


def session_color(name):
    return SESSION_COLORS[zlib.crc32(name.encode('utf-8')) % len(SESSION_COLORS)]


def tool_label(name):
    if name in TOOLS:
        return TOOLS[name]
    if name.startswith('mcp__'):
        return 'MCP', ORANGE
    return name[:5].upper(), DIM


def describe_call(name, args):
    """Human-readable summary of a tool call."""
    args = args or {}
    if name in ('Bash', 'PowerShell'):
        return args.get('description') or args.get('command', '')
    if name in ('Read', 'Edit', 'Write', 'NotebookEdit'):
        return Path(str(args.get('file_path', ''))).name
    if name in ('Grep', 'Glob'):
        return args.get('pattern', '')
    if name == 'Agent':
        return args.get('description') or args.get('prompt', '')
    if name == 'WebSearch':
        return args.get('query', '')
    if name == 'WebFetch':
        return args.get('url', '')
    if name == 'Skill':
        return args.get('skill', '')
    if name == 'AskUserQuestion':
        questions = args.get('questions') or [{}]
        return questions[0].get('question', '') if isinstance(questions[0], dict) else ''
    if name == 'Artifact':
        return args.get('action') or 'publish'
    if name.startswith('mcp__'):
        return name.split('__')[-1]
    for v in args.values():
        if isinstance(v, str) and v:
            return v
    return ''


# --- session transcript ------------------------------------------------------------

class Transcript:
    """Tails a session .jsonl as it grows and keeps a summary of the current turn."""

    TAIL = 3_000_000

    def __init__(self, path):
        self.path = path
        self.offset = None
        self.title = ''
        self.prompt = ''
        self.prompt_at = None
        self.answer = ''
        self.answer_at = None
        self.steps = []            # steps of the current turn: [tool, description, start, end]
        self.by_id = {}
        self.last = None
        self.tokens = 0
        self.ticks = deque(maxlen=5000)
        self.events = deque(maxlen=40)

    def update(self):
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        partial_first_line = False
        if self.offset is None:
            self.offset = max(0, size - self.TAIL)
            partial_first_line = self.offset > 0
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return
        with open(self.path, 'rb') as f:
            f.seek(self.offset)
            data = f.read(size - self.offset)
        end = data.rfind(b'\n')
        if end < 0:
            return
        data = data[: end + 1]
        self.offset += len(data)
        lines = data.split(b'\n')
        if partial_first_line:
            lines = lines[1:]
        for line in lines:
            if line.strip():
                try:
                    self.parse(json.loads(line))
                except Exception:
                    pass

    def parse(self, d):
        kind = d.get('type')
        if kind == 'ai-title':
            self.title = d.get('aiTitle') or self.title
            return
        if kind == 'last-prompt' and not self.prompt:
            self.prompt = d.get('lastPrompt') or ''
            return
        if kind not in ('user', 'assistant') or d.get('isSidechain'):
            return
        t = parse_iso(d.get('timestamp', '')) or now()
        self.last = max(self.last or 0, t)
        self.ticks.append(t)
        message = d.get('message') or {}
        content = message.get('content')
        if kind == 'user':
            if isinstance(content, str):
                text = content
            else:
                text = ''
                for block in content or []:
                    if block.get('type') == 'tool_result':
                        step = self.by_id.pop(block.get('tool_use_id'), None)
                        if step:
                            step[3] = t
                    elif block.get('type') == 'text':
                        text += block.get('text', '')
            if text and not d.get('isMeta') and not text.lstrip().startswith('<'):
                self.prompt, self.prompt_at = text, t
                self.answer, self.answer_at = '', None
                self.steps, self.by_id = [], {}
                self.events.append((t, 'you', short(text, 200)))
            return
        usage = message.get('usage') or {}
        tokens = sum(usage.get(k) or 0 for k in
                     ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
        if tokens:
            self.tokens = tokens + (usage.get('output_tokens') or 0)
        for block in content or []:
            if block.get('type') == 'tool_use':
                name = block.get('name', '?')
                step = [name, describe_call(name, block.get('input')), t, None]
                self.steps.append(step)
                self.by_id[block.get('id')] = step
                self.events.append((t, name, short(step[1], 200)))
            elif block.get('type') == 'text' and block.get('text', '').strip():
                self.answer, self.answer_at = block['text'], t
                self.events.append((t, 'claude', short(block['text'], 200)))

    def running_step(self):
        running = [s for s in self.steps if s[3] is None]
        return running[-1] if running else None

    def chart(self, width):
        t_now = now()
        buckets = [0] * width
        step = CHART_MINUTES * 60 / width
        for t in self.ticks:
            i = int((t_now - t) // step)
            if 0 <= i < width:
                buckets[width - 1 - i] += 1
        top = max(buckets) or 1
        return ''.join(' ' if c == 0 else BARS[min(7, max(0, round(c / top * 7)))] for c in buckets)


# --- processes and windows -----------------------------------------------------------

def project_slug(folder):
    """Claude Code stores transcripts under ~/.claude/projects/<folder with non-alnum chars as '-'>."""
    return re.sub(r'[^A-Za-z0-9]', '-', folder)


def find_transcript(folder, session_id):
    path = CLAUDE_HOME / 'projects' / project_slug(folder) / f'{session_id}.jsonl'
    if path.exists():
        return path
    for p in (CLAUDE_HOME / 'projects').glob(f'*/{session_id}.jsonl'):
        return p
    return path


def window_sockets():
    """label -> WezTerm socket path, from state/wezterm-<label>.txt written by the start scripts."""
    result = {}
    for f in sorted(STATE_DIR.glob('wezterm-*.txt')):
        try:
            result[f.stem.split('-', 1)[1]] = f.read_text(encoding='utf-8-sig').strip()
        except OSError:
            pass
    return result


def window_labels():
    """pid of a wezterm-gui window -> its label (remote / launcher / monitor)."""
    labels = {}
    for label, sock in window_sockets().items():
        try:
            labels[int(sock.rsplit('-', 1)[1])] = label
        except (ValueError, IndexError):
            pass
    return labels


def arg_after(cmd, key):
    for i, a in enumerate(cmd[:-1]):
        if a == key:
            return cmd[i + 1]
    return None


def is_claude_process(name, cmd):
    name = (name or '').lower()
    return name in ('claude.exe', 'claude') or (name in ('node.exe', 'node') and any('claude' in a for a in cmd))


def origin(pid, windows):
    """(window, environment, is_remote) of a session process, from its parent chain."""
    window, env, remote = 'outside WezTerm', None, False
    try:
        p = psutil.Process(pid)
        cmd = p.cmdline()
        if '--remote-control' in cmd:
            env = arg_after(cmd, '--remote-control')
            remote = True
        for parent in p.parents():
            name = parent.name().lower()
            if name == 'wezterm-gui.exe':
                window = windows.get(parent.pid, 'WezTerm')
                break
            if name == 'claude.exe' and 'WindowsApps' in (parent.exe() or ''):
                window = 'Claude app'
                break
            if name == 'code.exe':
                window = 'VS Code'
                break
            try:
                parent_cmd = parent.cmdline()
            except Exception:
                continue
            if 'remote-control' in parent_cmd and env is None:
                env = arg_after(parent_cmd, '--name')
                remote = True
    except Exception:
        pass
    return window, env, remote


class Session:
    def __init__(self, pid, record, transcript, window, env, remote):
        self.pid, self.record, self.transcript = pid, record, transcript
        self.window, self.env, self.remote = window, env, remote
        self.name = env or Path(record.get('cwd', '')).name or record.get('name', '?')
        self.color = session_color(self.name)
        tr, status = transcript, record.get('status')
        if status == 'busy':
            self.state = WORKING
        elif status not in (None, 'idle'):
            self.state = ATTENTION
        elif not tr.prompt:
            self.state = FRESH
        elif tr.answer_at and now() - tr.answer_at < YOUR_TURN_MINUTES * 60:
            self.state = YOUR_TURN
        else:
            self.state = QUIET

    @property
    def turn_started(self):
        s = (self.record.get('statusUpdatedAt') or 0) / 1000
        if self.transcript.prompt_at:
            s = min(s, self.transcript.prompt_at) if s else self.transcript.prompt_at
        return s or now()


class Collector:
    def __init__(self):
        self.transcripts, self.origins, self.windows = {}, {}, {}
        self.tabs, self.services = {}, {}
        self.gpu = None
        self.history = {'cpu': deque(maxlen=60), 'ram': deque(maxlen=60), 'gpu': deque(maxlen=60)}
        self.running_envs, self.persistent = set(), []
        self._slow_at = 0
        psutil.cpu_percent()
        threading.Thread(target=self._load_loop, daemon=True).start()
        threading.Thread(target=self._background, daemon=True).start()

    def _load_loop(self):
        while True:
            self.history['cpu'].append(psutil.cpu_percent())
            self.history['ram'].append(psutil.virtual_memory().percent)
            if self.gpu:
                self.history['gpu'].append(self.gpu[0])
            time.sleep(1)

    def _background(self):
        while True:
            self.services = {name: self._port_open(port) for name, port in SERVICES}
            self.gpu = self._gpu()
            self.tabs = self._tabs()
            time.sleep(2)

    @staticmethod
    def _port_open(port):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                return True
        except OSError:
            return False

    @staticmethod
    def _gpu():
        try:
            out = subprocess.run(
                ['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total',
                 '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=3,
                creationflags=NO_WINDOW).stdout
            used, mem, total = (int(x) for x in out.splitlines()[0].split(','))
            return used, mem, total
        except Exception:
            return None

    @staticmethod
    def _tabs():
        result = {}
        for label, sock in window_sockets().items():
            try:
                env = dict(os.environ, WEZTERM_UNIX_SOCKET=sock)
                out = subprocess.run([WEZTERM, 'cli', '--no-auto-start', 'list', '--format', 'json'],
                                     capture_output=True, timeout=3, env=env, creationflags=NO_WINDOW).stdout
                result[label] = len({p['tab_id'] for p in json.loads(out or b'[]')}) or None
            except Exception:
                result[label] = None
        return result

    def collect(self):
        if now() - self._slow_at > 3:
            self._slow_at = now()
            self.windows = window_labels()
            self.persistent = []
            try:
                for line in PROJECTS_FILE.read_text(encoding='utf-8-sig').splitlines():
                    if line.strip() and not line.lstrip().startswith('#') and '|' in line:
                        self.persistent.append(line.split('|', 1)[0].strip())
            except OSError:
                pass
            self.running_envs = set()
            for p in psutil.process_iter(['name', 'cmdline']):
                try:
                    cmd = p.info['cmdline'] or []
                    if 'remote-control' in cmd and is_claude_process(p.info['name'], cmd):
                        self.running_envs.add(arg_after(cmd, '--name'))
                except Exception:
                    pass

        sessions = []
        for f in (CLAUDE_HOME / 'sessions').glob('*.json'):
            try:
                pid = int(f.stem)
                if not psutil.pid_exists(pid):
                    continue
                record = json.loads(f.read_text(encoding='utf-8'))
            except Exception:
                continue
            sid = record.get('sessionId')
            if sid not in self.transcripts:
                self.transcripts[sid] = Transcript(find_transcript(record.get('cwd', ''), sid))
            transcript = self.transcripts[sid]
            transcript.update()
            if pid not in self.origins:
                self.origins[pid] = origin(pid, self.windows)
            sessions.append(Session(pid, record, transcript, *self.origins[pid]))
        sessions.sort(key=lambda s: (ORDER[s.state], -(s.transcript.last or 0)))
        return sessions


# --- widgets -------------------------------------------------------------------------

def frame(t, speed=10):
    return SPINNER[int(t * speed) % len(SPINNER)]


def runner(width, t, color=ACCENT):
    """A highlight running along a line: the process is alive and working."""
    pos = int(t * 14) % (width * 2)
    pos = pos if pos < width else 2 * width - pos - 1
    text = Text()
    for i in range(width):
        d = abs(i - pos)
        text.append('━', style=color if d == 0 else ('#9a9630' if d <= 2 else ('#4a4820' if d <= 5 else '#1c1c1c')))
    return text


def meter(share, width, color):
    share = max(0.0, min(1.0, share))
    filled = share * width
    whole = int(filled)
    part = ' ▏▎▍▌▋▊▉'[int((filled - whole) * 8)] if whole < width else ''
    t = Text('█' * whole, style=color)
    if part.strip():
        t.append(part, style=color)
        whole += 1
    t.append('█' * (width - whole), style='#1f2329')
    return t


def load_color(p):
    return GREEN if p < 60 else (ACCENT if p < 85 else RED)


def sparkline(values, width):
    values = list(values)[-width:]
    if not values:
        return Text(' ' * width)
    t = Text(' ' * (width - len(values)))
    for v in values:
        t.append(BARS[min(7, int(v / 100 * 7.99))], style=load_color(v))
    return t


def chip(text, bg, fg='black'):
    return Text(f' {text} ', style=f'bold {fg} on {bg}')


def labeled(label, label_style, text, text_style):
    """'you ...' / 'reply ...': label in its own column, wrapped text stays aligned."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=5, no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(Text(label, style=label_style), Text(text, style=text_style))
    return grid


def line(*parts):
    t = Text(no_wrap=True, overflow='ellipsis')
    for p in parts:
        t.append_text(p) if isinstance(p, Text) else t.append(p)
    return t


# --- cards ---------------------------------------------------------------------------

def card_subtitle(s):
    where = s.window + (' · remote' if s.remote and s.window != 'remote' else '')
    return Text(f' {where} · pid {s.pid} ', style=DIM)


def card_title(s):
    t = Text()
    t.append(' ● ', style=s.color)
    t.append(f'{s.name} ', style=f'bold {BRIGHT}')
    return t


def card_footer(s, inner, chart_color):
    tr = s.transcript
    t = Text(no_wrap=True)
    t.append(tr.chart(max(10, inner - 30)), style=chart_color)
    t.append(f' {CHART_MINUTES}m ', style=DARKER)
    if tr.tokens:
        t.append(' ctx ', style=DIM)
        t.append_text(meter(tr.tokens / CONTEXT_TOKENS, 8, '#5e81ac'))
        t.append(f' {tr.tokens / 1000:.0f}k', style=DIM)
    return t


def working_card(s, t, inner):
    """Large card: the session is working right now."""
    tr = s.transcript
    t_now = now()
    rows = []

    top = Text(no_wrap=True)
    top.append_text(chip(f'{frame(t)} WORKING', ACCENT))
    top.append(f'  {clock(t_now - s.turn_started)}', style=f'bold {ACCENT}')
    if tr.steps:
        top.append(f'   step {len(tr.steps)}', style=DIM)
    agents = sum(1 for st in tr.steps if st[0] == 'Agent' and st[3] is None)
    if agents:
        top.append(f'   agents {agents}', style='bold #b48ead')
    rows.append(top)

    if tr.title:
        rows.append(Text(short(tr.title, inner), style=f'bold {BRIGHT}', no_wrap=True))
    if tr.prompt:
        rows.append(labeled('you', f'bold {s.color}', short(tr.prompt, (inner - 6) * 2 - 6), TEXT))
    rows.append(Text(''))

    # the last steps of the turn; the running one is highlighted
    visible = tr.steps[-6:]
    if len(tr.steps) > 6:
        rows.append(Text(f'   ⋮ {len(tr.steps) - 6} earlier steps', style=DARKER))
    running = tr.running_step()
    for name, desc, start, end in visible:
        label, color = tool_label(name)
        r = Text(no_wrap=True, overflow='ellipsis')
        if end is None:
            took = duration(t_now - start)
            r.append(f' {frame(t, 12)} ', style=f'bold {ACCENT}')
            r.append(f'{label:<5} ', style=f'bold {color}')
            r.append(short(desc, inner - 17 - len(took)), style=f'bold {BRIGHT}')
            r.pad_right(max(0, inner - r.cell_len - len(took)))
            r.append(took, style=f'bold {ACCENT}')
        else:
            took = duration(end - start)
            r.append(' ✓ ', style=GREEN)
            r.append(f'{label:<5} ', style=color)
            r.append(short(desc, inner - 17 - len(took)), style=DIM)
            r.pad_right(max(0, inner - r.cell_len - len(took)))
            r.append(took, style=DARKER)
        rows.append(r)
    if not running:
        rows.append(line(Text(f' {frame(t, 12)} ', style=f'bold {ACCENT}'),
                         Text('thinking and writing…', style=f'italic {ACCENT}')))
    rows.append(Text(''))
    rows.append(runner(inner, t + s.pid % 5))
    rows.append(card_footer(s, inner, ACCENT))
    return Panel(Group(*rows), title=card_title(s), title_align='left', subtitle=card_subtitle(s),
                 subtitle_align='right', border_style=ACCENT, padding=(0, 1))


def resting_card(s, inner):
    """Medium card: your turn, quiet or attention."""
    tr = s.transcript
    t_now = now()
    if s.state == YOUR_TURN:
        color, badge, border, answer_style = GREEN, chip('✔ YOUR TURN', GREEN), '#3f5a3a', TEXT
    elif s.state == ATTENTION:
        color, badge = ORANGE, chip(f'! {s.record.get("status")}', ORANGE)
        border, answer_style = ORANGE, TEXT
    else:
        color, badge, border, answer_style = DIM, Text(' ◌ quiet ', style=DIM), BORDER, DIM
    top = Text(no_wrap=True)
    top.append_text(badge)
    when = tr.answer_at or tr.last
    if when:
        top.append(f'  {ago(t_now - when)} ago', style=color)
    if tr.steps and tr.prompt_at and when:
        top.append(f'  · {len(tr.steps)} step{"s" if len(tr.steps) != 1 else ""} in {clock(when - tr.prompt_at)}', style=DIM)
    rows = [top]
    if tr.title:
        rows.append(Text(short(tr.title, inner), style=f'bold {BRIGHT if s.state != QUIET else TEXT}', no_wrap=True))
    if tr.prompt:
        rows.append(labeled('you', f'bold {s.color}', short(tr.prompt, inner - 6), DIM))
    if tr.answer:
        rows.append(labeled('reply', f'bold {color}' if s.state != QUIET else DARKER,
                            short(tr.answer, (inner - 6) * 2 - 8), answer_style))
    rows.append(card_footer(s, inner, color if s.state != QUIET else DARKER))
    return Panel(Group(*rows), title=card_title(s), title_align='left', subtitle=card_subtitle(s),
                 subtitle_align='right', border_style=border, padding=(0, 1))


def grid(panels, columns):
    table = Table.grid(padding=(0, 1), expand=True)
    for _ in range(columns):
        table.add_column(ratio=1)
    for i in range(0, len(panels), columns):
        row = panels[i:i + columns]
        table.add_row(*row, *[''] * (columns - len(row)))
    return table


def section(title, count, color):
    t = Text(no_wrap=True)
    t.append(f' {title} ', style=f'bold {color}')
    t.append(f'{count}', style=DIM)
    return t


# --- screen --------------------------------------------------------------------------

def header(sessions, t, width):
    count = {k: sum(1 for s in sessions if s.state == k) for k in ORDER}
    h = Text(no_wrap=True, overflow='ellipsis', style=f'on {HEADER_BG}')
    h.append(' ◆ CLAUDE SESSIONS ', style=f'bold black on {ACCENT}')
    h.append(f'  {datetime.now():%H:%M:%S}  ', style=f'bold {BRIGHT} on {HEADER_BG}')

    def counter(icon, n, name, color):
        h.append('   ')
        h.append(f'{icon} {n}', style=f'bold {color if n else DARKER} on {HEADER_BG}')
        h.append(f' {name}', style=f'{color if n else DARKER} on {HEADER_BG}')
    if count[ATTENTION]:
        counter('!', count[ATTENTION], 'attention', ORANGE)
    counter(frame(t) if count[WORKING] else '▶', count[WORKING], 'working', ACCENT)
    counter('✔', count[YOUR_TURN], 'waiting for you', GREEN)
    counter('◌', count[QUIET], 'quiet', DIM)
    counter('○', count[FRESH], 'fresh', DIM)
    h.pad_right(max(0, width - h.cell_len))
    return h


def footer():
    t = Text(no_wrap=True, overflow='ellipsis', style=DARKER)
    for icon, name, color in (('▶', 'working', ACCENT), ('✔', 'answered — your turn', GREEN),
                              ('◌', f'quiet > {YOUR_TURN_MINUTES} min', DIM), ('○', 'no prompts yet', DIM),
                              ('✖', 'remote environment down', RED)):
        t.append(f' {icon} ', style=color)
        t.append(f'{name}  ', style=DARKER)
    t.append('  Ctrl+C to quit', style=DARKER)
    return t


def main_area(sessions, col, t, width):
    parts = []
    working = [s for s in sessions if s.state == WORKING]
    resting = [s for s in sessions if s.state in (ATTENTION, YOUR_TURN, QUIET)]
    fresh = [s for s in sessions if s.state == FRESH]

    if working:
        columns = 1 if width < 110 or len(working) == 1 and width < 150 else min(len(working), 2 if width < 200 else 3)
        inner = (width - (columns - 1)) // columns - 4
        parts.append(section('WORKING NOW', len(working), ACCENT))
        parts.append(grid([working_card(s, t, inner) for s in working], columns))
    else:
        parts.append(Panel(Text.assemble(('  nothing is running', f'bold {DIM}'),
                                         ('  — all sessions are at rest', DARKER)),
                           border_style=BORDER, padding=(0, 1)))

    if resting:
        columns = max(1, min(3, width // 60))
        inner = (width - (columns - 1)) // columns - 4
        waiting = sum(1 for s in resting if s.state == YOUR_TURN)
        parts.append(Text(''))
        parts.append(section('WAITING FOR YOU' if waiting else 'RECENT', len(resting), GREEN if waiting else DIM))
        parts.append(grid([resting_card(s, inner) for s in resting], columns))

    down = [name for name in col.persistent if name not in col.running_envs]
    if fresh or down:
        parts.append(Text(''))
        t_ = Text()
        t_.append(' FRESH ', style=f'bold {DIM}')
        for s in fresh:
            t_.append('  ○ ', style=s.color)
            t_.append(s.name, style=TEXT)
            if s.window != 'remote':
                t_.append(f' ({s.window})', style=DARKER)
        for name in down:
            t_.append('  ✖ ', style=f'bold {RED}')
            t_.append(f'{name} — not running', style=f'bold {RED}')
        parts.append(Panel(t_, border_style=BORDER, padding=(0, 0)))
    return Group(*parts)


def system_panel(col, width):
    w = width - 4
    bar_w, spark_w = max(8, w - 26), 10
    table = Table.grid(padding=(0, 1))
    table.add_column(width=4, style=DIM)
    table.add_column()
    table.add_column(width=5, justify='right')
    table.add_column(width=spark_w)

    def row(name, p, history):
        table.add_row(name, meter(p / 100, bar_w, load_color(p)),
                      Text(f'{p:.0f}%', style=f'bold {load_color(p)}'), sparkline(history, spark_w))
    cpu = col.history['cpu'][-1] if col.history['cpu'] else 0
    ram = col.history['ram'][-1] if col.history['ram'] else 0
    row('CPU', cpu, col.history['cpu'])
    row('RAM', ram, col.history['ram'])
    if col.gpu:
        used, mem, total = col.gpu
        row('GPU', used, col.history['gpu'])
        table.add_row('VRAM', meter(mem / total, bar_w, '#5e81ac'),
                      Text(f'{mem / 1024:.0f}/{total / 1024:.0f}', style=DIM), Text('GB', style=DARKER))

    services = Text(no_wrap=True)
    for name, alive in col.services.items():
        services.append(' ● ' if alive else ' ○ ', style=GREEN if alive else DARKER)
        services.append(name, style=TEXT if alive else DARKER)
    windows = Text(no_wrap=True)
    for name, n in col.tabs.items():
        windows.append(' ▣ ', style=DIM if n else RED)
        windows.append(name, style=TEXT if n else RED)
        windows.append(f' {n}' if n else ' gone', style=DIM if n else RED)
    return Panel(Group(table, Text(''), services, windows),
                 title=Text(' SYSTEM ', style=f'bold {TEXT}'), title_align='left',
                 border_style=BORDER, padding=(0, 1))


def feed(sessions, width, height):
    events, seen = [], set()
    for s in sessions:
        if id(s.transcript) in seen:   # two processes can share one session (remote-control parent and child)
            continue
        seen.add(id(s.transcript))
        for t, kind, text in s.transcript.events:
            events.append((t, s, kind, text))
    events.sort(key=lambda e: e[0], reverse=True)
    w = width - 4
    rows = []
    for t, s, kind, text in events[: max(1, height - 2)]:
        r = Text(no_wrap=True, overflow='ellipsis')
        r.append(datetime.fromtimestamp(t).strftime('%H:%M '), style=DARKER)
        r.append(f'{short(s.name, 9):<9} ', style=s.color)
        if kind == 'you':
            r.append('YOU   ', style=f'bold {s.color}')
            r.append(short(text, w - 22), style=BRIGHT)
        elif kind == 'claude':
            r.append('REPLY ', style=f'bold {GREEN}')
            r.append(short(text, w - 22), style=TEXT)
        else:
            label, color = tool_label(kind)
            r.append(f'{label:<5} ', style=color)
            r.append(short(text, w - 22), style=DIM)
        rows.append(r)
    if not rows:
        rows.append(Text('nothing yet', style=DARKER))
    return Panel(Group(*rows), title=Text(' FEED ', style=f'bold {TEXT}'), title_align='left',
                 border_style=BORDER, padding=(0, 1))


def screen(col, console):
    t = now()
    sessions = col.collect()
    width, height = console.size
    root = Layout()
    root.split_column(Layout(name='header', size=2), Layout(name='body'), Layout(name='footer', size=1))
    root['header'].update(Group(header(sessions, t, width), Text('')))
    root['footer'].update(footer())
    if width >= 130:
        side = min(SIDEBAR_WIDTH, width // 3)
        root['body'].split_row(Layout(name='main'), Layout(name='side', size=side))
        root['main'].update(main_area(sessions, col, t, width - side - 1))
        system_height = (10 if col.gpu else 8) - (0 if col.services else 1)
        root['side'].split_column(Layout(name='system', size=system_height), Layout(name='feed'))
        root['system'].update(system_panel(col, side))
        root['feed'].update(feed(sessions, side, height - 3 - system_height))
    else:
        root['body'].split_column(Layout(name='main', ratio=3), Layout(name='feed', ratio=1, minimum_size=6))
        root['main'].update(main_area(sessions, col, t, width))
        root['feed'].update(feed(sessions, width, max(6, (height - 3) // 4)))
    return root


def main():
    # Force truecolor and VT sequences, otherwise rich draws without color under WezTerm/conpty.
    # NO_COLOR may be inherited (for example from a Claude session); the monitor always wants color.
    os.environ.pop('NO_COLOR', None)
    console = Console(force_terminal=True, color_system='truecolor', legacy_windows=False, no_color=False)
    if os.name == 'nt':
        os.system('title monitor')
    col = Collector()
    with Live(screen(col, console), console=console, screen=True, auto_refresh=False) as live:
        while True:
            live.update(screen(col, console), refresh=True)
            time.sleep(0.1)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
