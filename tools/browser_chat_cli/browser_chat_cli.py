#!/usr/bin/env python3
# Copyright 2025 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""CLI tool for chatting via an open browser tab.

Connects to a running Chrome instance using the Chrome DevTools Protocol (CDP),
injects a text message into the chat input field of a browser tab, sends it,
and waits for the reply to appear in the conversation.  Works with any web
page that has a chat-like interface.

Prerequisite – start Chrome with remote debugging enabled:
  chrome --remote-debugging-port=9222

Usage examples:
  # List all open tabs to find the one to target:
  python3 browser_chat_cli.py --list-tabs

  # Send one message and print the response:
  python3 browser_chat_cli.py --message "Hello!" --tab "my-chat-site.com"

  # Interactive (REPL) chat mode:
  python3 browser_chat_cli.py --interactive --tab "my-chat-site.com"

  # Use explicit CSS selectors for precision:
  python3 browser_chat_cli.py --message "Hi" --tab "my-chat-site.com" \\
      --input-selector "div[contenteditable='true']" \\
      --response-selector ".message-text"
"""

import argparse
import base64
import hashlib
import json
import random
import socket
import struct
import sys
import threading
import time
import urllib.request
from typing import Callable, Optional

# WebSocket GUID from RFC 6455.
_WS_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

# Default Chrome remote debugging port.
_DEFAULT_PORT = 9222

# Default seconds to wait for a response before giving up.
_DEFAULT_RESPONSE_TIMEOUT = 30.0

# How often to re-evaluate the DOM when polling for a new reply (seconds).
# 250 ms balances responsiveness with CDP round-trip overhead: polling faster
# than ~100 ms saturates the CDP channel; polling slower than ~500 ms makes
# the tool feel unresponsive to the user.
_POLL_INTERVAL_SECONDS = 0.25


class BrowserChatError(Exception):
    """Base exception for all browser_chat_cli errors."""


class ConnectionError(BrowserChatError):
    """Raised when the tool cannot connect to Chrome."""


class TabNotFoundError(BrowserChatError):
    """Raised when no suitable tab can be located."""


class InjectionError(BrowserChatError):
    """Raised when message injection or sending fails."""


class ResponseTimeoutError(BrowserChatError):
    """Raised when no response is received within the timeout period."""


# ---------------------------------------------------------------------------
# Minimal WebSocket client (RFC 6455) using only Python stdlib.
# ---------------------------------------------------------------------------

class _WebSocketClient:
    """Minimal WebSocket client over a raw TCP socket.

    Implements enough of RFC 6455 to communicate with Chrome's CDP endpoint.
    Client frames are always masked as required by the specification.
    """

    # WebSocket opcodes.
    _OP_TEXT = 0x1
    _OP_BINARY = 0x2
    _OP_CLOSE = 0x8
    _OP_PING = 0x9
    _OP_PONG = 0xA

    def __init__(self, host: str, port: int, path: str):
        self._host = host
        self._port = port
        self._path = path
        self._sock: Optional[socket.socket] = None
        self._recv_buf = b''
        self._send_lock = threading.Lock()

    def connect(self):
        """Open the TCP connection and perform the HTTP upgrade handshake."""
        self._sock = socket.create_connection((self._host, self._port),
                                              timeout=10)
        self._sock.settimeout(60.0)
        self._do_handshake()

    def close(self):
        """Send a WebSocket close frame and shut down the socket."""
        if self._sock is None:
            return
        try:
            self._send_raw_frame(b'', self._OP_CLOSE)
            self._sock.close()
        except OSError:
            pass
        finally:
            self._sock = None

    def send_text(self, data: str):
        """Send a UTF-8 text frame."""
        with self._send_lock:
            self._send_raw_frame(data.encode('utf-8'), self._OP_TEXT)

    def recv_text(self, timeout: float = 1.0) -> Optional[str]:
        """Receive one text frame.  Returns None if timeout expires."""
        if self._sock is None:
            return None
        self._sock.settimeout(timeout)
        try:
            payload = self._recv_one_frame()
            if payload is None:
                return None
            return payload.decode('utf-8')
        except (socket.timeout, TimeoutError):
            return None

    # -- private helpers -----------------------------------------------------

    def _do_handshake(self):
        nonce = base64.b64encode(bytes(random.getrandbits(8)
                                       for _ in range(16))).decode('ascii')
        request = (f'GET {self._path} HTTP/1.1\r\n'
                   f'Host: {self._host}:{self._port}\r\n'
                   f'Upgrade: websocket\r\n'
                   f'Connection: Upgrade\r\n'
                   f'Sec-WebSocket-Key: {nonce}\r\n'
                   f'Sec-WebSocket-Version: 13\r\n'
                   f'\r\n')
        self._sock.sendall(request.encode('ascii'))

        # Read until the end of the HTTP response headers.
        response = b''
        while b'\r\n\r\n' not in response:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise BrowserChatError('Connection closed during WS handshake')
            response += chunk

        headers_bytes, _, leftover = response.partition(b'\r\n\r\n')
        self._recv_buf = leftover

        first_line = headers_bytes.split(b'\r\n', 1)[0]
        if b' 101 ' not in first_line:
            raise BrowserChatError(
                f'WebSocket upgrade failed: {first_line.decode(errors="replace")}')

        expected_accept = base64.b64encode(
            hashlib.sha1(
                nonce.encode('ascii') + _WS_GUID).digest()).decode('ascii')
        for raw_line in headers_bytes.split(b'\r\n'):
            line = raw_line.decode('ascii', errors='replace')
            if line.lower().startswith('sec-websocket-accept:'):
                server_accept = line.split(':', 1)[1].strip()
                if server_accept != expected_accept:
                    raise BrowserChatError('WebSocket accept-key mismatch')
                return
        raise BrowserChatError('Missing Sec-WebSocket-Accept header')

    def _send_raw_frame(self, data: bytes, opcode: int):
        """Encode and send one masked WebSocket frame."""
        length = len(data)
        mask = bytes(random.getrandbits(8) for _ in range(4))
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += struct.pack('!BH', 0x80 | 126, length)
        else:
            header += struct.pack('!BQ', 0x80 | 127, length)
        header += mask
        masked_data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._sock.sendall(header + masked_data)

    def _recv_one_frame(self) -> Optional[bytes]:
        """Read frames from the socket until we receive a data frame."""
        while True:
            # Ensure we have at least 2 header bytes.
            while len(self._recv_buf) < 2:
                chunk = self._sock.recv(4096)
                if not chunk:
                    return None
                self._recv_buf += chunk

            b0 = self._recv_buf[0]
            b1 = self._recv_buf[1]
            opcode = b0 & 0x0F
            is_masked = bool(b1 & 0x80)
            payload_len = b1 & 0x7F

            header_size = 2
            if payload_len == 126:
                header_size += 2
            elif payload_len == 127:
                header_size += 8
            if is_masked:
                header_size += 4

            while len(self._recv_buf) < header_size:
                chunk = self._sock.recv(4096)
                if not chunk:
                    return None
                self._recv_buf += chunk

            offset = 2
            if payload_len == 126:
                payload_len = struct.unpack_from('!H', self._recv_buf,
                                                 offset)[0]
                offset += 2
            elif payload_len == 127:
                payload_len = struct.unpack_from('!Q', self._recv_buf,
                                                 offset)[0]
                offset += 8

            mask_bytes = b''
            if is_masked:
                mask_bytes = self._recv_buf[offset:offset + 4]
                offset += 4

            total = offset + payload_len
            while len(self._recv_buf) < total:
                chunk = self._sock.recv(4096)
                if not chunk:
                    return None
                self._recv_buf += chunk

            payload = bytearray(self._recv_buf[offset:total])
            self._recv_buf = self._recv_buf[total:]

            if is_masked:
                for i, byte in enumerate(payload):
                    payload[i] = byte ^ mask_bytes[i % 4]

            if opcode == self._OP_CLOSE:
                return None
            if opcode == self._OP_PING:
                # Reply with a pong.
                with self._send_lock:
                    self._send_raw_frame(bytes(payload), self._OP_PONG)
                continue
            if opcode in (self._OP_TEXT, self._OP_BINARY):
                return bytes(payload)
            # Ignore other control frames and continue.


# ---------------------------------------------------------------------------
# CDP session over WebSocket.
# ---------------------------------------------------------------------------

class _CDPSession:
    """A Chrome DevTools Protocol session.

    Sends JSON-encoded CDP commands over a _WebSocketClient and dispatches
    responses and events to waiting callers.
    """

    def __init__(self, ws: _WebSocketClient):
        self._ws = ws
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._pending_lock = threading.Lock()
        self._events: list[dict] = []
        self._events_lock = threading.Lock()
        self._recv_thread = threading.Thread(target=self._recv_loop,
                                             name='cdp-recv',
                                             daemon=True)
        self._recv_thread.start()

    def send(self, method: str, params: Optional[dict] = None) -> dict:
        """Send a CDP command and return the response dict (blocking)."""
        with self._id_lock:
            msg_id = self._next_id
            self._next_id += 1

        message: dict = {'id': msg_id, 'method': method}
        if params:
            message['params'] = params

        done_event = threading.Event()
        holder: dict = {'response': None, 'event': done_event}
        with self._pending_lock:
            self._pending[msg_id] = holder

        self._ws.send_text(json.dumps(message))

        if not done_event.wait(timeout=30.0):
            raise BrowserChatError(
                f'Timed out waiting for CDP response to: {method}')
        return holder['response']

    def wait_for_event(self,
                       event_name: str,
                       predicate: Optional[Callable[[dict], bool]] = None,
                       timeout: float = _DEFAULT_RESPONSE_TIMEOUT
                       ) -> Optional[dict]:
        """Block until a matching event arrives, or timeout expires."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._events_lock:
                for i, evt in enumerate(self._events):
                    if evt.get('method') == event_name:
                        params = evt.get('params', {})
                        if predicate is None or predicate(params):
                            self._events.pop(i)
                            return evt
            time.sleep(0.05)
        return None

    def _recv_loop(self):
        """Background thread: receive CDP messages and dispatch them."""
        while True:
            raw = self._ws.recv_text(timeout=1.0)
            if raw is None:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if 'id' in obj:
                with self._pending_lock:
                    holder = self._pending.pop(obj['id'], None)
                if holder:
                    holder['response'] = obj
                    holder['event'].set()
            elif 'method' in obj:
                with self._events_lock:
                    self._events.append(obj)


# ---------------------------------------------------------------------------
# Tab discovery.
# ---------------------------------------------------------------------------

def list_tabs(port: int) -> list[dict]:
    """Return all open tabs from Chrome's JSON endpoint.

    Args:
        port: The remote debugging port Chrome was started with.

    Returns:
        A list of tab descriptor dicts as returned by /json.

    Raises:
        ConnectionError: If Chrome is not reachable on the given port.
    """
    url = f'http://localhost:{port}/json'
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        raise ConnectionError(
            f'Cannot connect to Chrome on port {port}.\n'
            f'Start Chrome with:\n'
            f'  chrome --remote-debugging-port={port}\n'
            f'  (add --user-data-dir=/tmp/chrome-debug if needed)\n'
            f'Original error: {exc}') from exc


def find_tab(tabs: list[dict],
             pattern: Optional[str] = None) -> Optional[dict]:
    """Find the target tab for chat injection.

    If *pattern* is given, the first page tab whose URL or title contains the
    pattern (case-insensitive) is returned.  If *pattern* is omitted, the
    first non-internal page tab is returned.

    Args:
        tabs: Tab list as returned by :func:`list_tabs`.
        pattern: Optional URL/title substring to match.

    Returns:
        The matching tab dict, or ``None`` if no tab is found.
    """
    page_tabs = [t for t in tabs if t.get('type') == 'page']

    if pattern:
        pattern_lower = pattern.lower()
        for tab in page_tabs:
            if (pattern_lower in tab.get('url', '').lower()
                    or pattern_lower in tab.get('title', '').lower()):
                return tab
        return None

    # Fall back to the first non-chrome/about page.
    for tab in page_tabs:
        url = tab.get('url', '')
        if not url.startswith(('about:', 'chrome:', 'devtools:')):
            return tab

    return page_tabs[0] if page_tabs else None


# ---------------------------------------------------------------------------
# JavaScript helpers.
# ---------------------------------------------------------------------------

def _js_snapshot_messages(response_selector: Optional[str]) -> str:
    """Return a JS expression that evaluates to an array of message texts."""
    if response_selector:
        sel = json.dumps(response_selector)
        return (f'Array.from(document.querySelectorAll({sel}))'
                f'.map(e => (e.innerText || e.textContent || "").trim())'
                f'.filter(Boolean)')

    # Generic heuristics that work for any chat-like page.
    return r"""
(function() {
  const candidates = [
    /* Standard ARIA role for chat message lists */
    '[role="listitem"] [dir]',
    '[role="row"] [dir]',
    /* Common class-name patterns used by chat UIs */
    '[class*="message"][class*="text"]',
    '[class*="message"][class*="content"]',
    '[class*="msg"][class*="text"]',
    '[class*="chat"][class*="bubble"]',
    '[class*="bubble"]',
    '[class*="chatMessage"]',
    '[class*="message"]',
  ];
  for (const sel of candidates) {
    const els = document.querySelectorAll(sel);
    if (els.length > 0) {
      return Array.from(els)
        .map(e => (e.innerText || e.textContent || '').trim())
        .filter(Boolean);
    }
  }
  return [];
})()
"""


def _js_inject_and_send(message: str,
                        input_selector: Optional[str] = None) -> str:
    """Return a JS async IIFE that injects *message* and triggers send."""
    escaped = json.dumps(message)
    if input_selector:
        find_input = f'document.querySelector({json.dumps(input_selector)})'
    else:
        find_input = r"""
(function() {
  const candidates = [
    /* Prefer ARIA textbox roles (used by most modern chat UIs) */
    'div[contenteditable="true"][role="textbox"]',
    /* Generic contenteditable divs (common in chat apps) */
    'div[contenteditable="true"]',
    /* Standard form elements */
    'textarea[placeholder*="message" i]',
    'textarea[placeholder*="chat" i]',
    'input[placeholder*="message" i]',
    'input[placeholder*="chat" i]',
    'textarea',
    'input[type="text"]',
  ];
  for (const sel of candidates) {
    const el = document.querySelector(sel);
    if (el) return el;
  }
  return document.activeElement;
})()"""

    return f"""
(async function() {{
  const el = {find_input};
  if (!el) throw new Error('Could not find a chat input element');

  el.focus();

  if (el.isContentEditable) {{
    // Use execCommand for contenteditable fields (WhatsApp, Telegram, etc.)
    document.execCommand('selectAll', false, null);
    document.execCommand('delete', false, null);
    document.execCommand('insertText', false, {escaped});
  }} else {{
    // For <input> / <textarea> elements use the native value setter so that
    // React/Vue input handlers are triggered correctly.
    const proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value');
    if (setter && setter.set) {{
      setter.set.call(el, {escaped});
    }} else {{
      el.value = {escaped};
    }}
    el.dispatchEvent(new Event('input', {{bubbles: true}}));
    el.dispatchEvent(new Event('change', {{bubbles: true}}));
  }}

  // Wait ~300 ms before submitting.  This duration was determined empirically:
  // chat apps attach React/Vue input handlers that update their internal
  // state asynchronously; submitting too quickly (< ~200 ms) can cause the
  // sent message to be empty even though the DOM shows the injected text.
  await new Promise(r => setTimeout(r, 300));

  // Press Enter to send.
  const keyOpts = {{
    key: 'Enter', code: 'Enter',
    keyCode: 13, which: 13,
    bubbles: true, cancelable: true,
  }};
  el.dispatchEvent(new KeyboardEvent('keydown', keyOpts));
  el.dispatchEvent(new KeyboardEvent('keypress', keyOpts));
  el.dispatchEvent(new KeyboardEvent('keyup', keyOpts));

  // Also click the send button if one is present.
  const sendSelectors = [
    '[data-testid="send"]',
    'button[aria-label*="send" i]',
    'button[data-testid*="send" i]',
    'button[title*="send" i]',
    '[class*="sendButton"]',
  ];
  for (const sel of sendSelectors) {{
    const btn = document.querySelector(sel);
    if (btn) {{ btn.click(); break; }}
  }}

  return 'ok';
}})()
"""


# ---------------------------------------------------------------------------
# Core send-and-receive logic.
# ---------------------------------------------------------------------------

def _extract_value_list(cdp_result: dict) -> list[str]:
    """Extract a list of strings from a CDP Runtime.evaluate result."""
    try:
        outer = cdp_result.get('result') or {}
        result_obj = outer.get('result') or {}
        # When returnByValue is True the full value is in 'value'.
        value = result_obj.get('value')
        if isinstance(value, list):
            return [str(v) for v in value if v]
        # When the array is large, CDP may return a preview instead.
        preview = result_obj.get('preview', {})
        if preview.get('subtype') == 'array':
            return [
                p['value'] for p in preview.get('properties', [])
                if isinstance(p.get('value'), str) and p['value']
            ]
    except (KeyError, TypeError):
        pass
    return []


def inject_message_and_wait(
        port: int,
        message: str,
        tab_pattern: Optional[str] = None,
        input_selector: Optional[str] = None,
        response_selector: Optional[str] = None,
        response_timeout: float = _DEFAULT_RESPONSE_TIMEOUT) -> str:
    """Inject *message* into a browser chat tab and return the reply.

    Steps:
      1. Discover tabs via Chrome's /json endpoint.
      2. Identify the target tab by *tab_pattern* or auto-detection.
      3. Open a WebSocket CDP session to that tab.
      4. Snapshot existing message texts.
      5. Inject *message* into the input field and trigger send.
      6. Poll for new message texts until one appears or *response_timeout*
         expires.
      7. Return the first new non-empty message text.

    Args:
        port: Chrome remote debugging port.
        message: Text to inject.
        tab_pattern: URL or title substring identifying the target tab.
        input_selector: CSS selector for the chat input element.
        response_selector: CSS selector for response message elements.
        response_timeout: Seconds to wait for a reply.

    Returns:
        The text of the first new message that appears after injection.

    Raises:
        ConnectionError: Cannot reach Chrome.
        TabNotFoundError: No suitable tab found.
        InjectionError: Injection or send failed.
        ResponseTimeoutError: No reply within *response_timeout* seconds.
    """
    tabs = list_tabs(port)
    tab = find_tab(tabs, tab_pattern)
    if tab is None:
        if tab_pattern:
            raise TabNotFoundError(f'No tab matching: {tab_pattern!r}')
        raise TabNotFoundError('No suitable browser tab found.  '
                               'Use --list-tabs to see available tabs.')

    ws_debugger_url: str = tab.get('webSocketDebuggerUrl', '')
    if not ws_debugger_url:
        raise TabNotFoundError(
            'Tab has no WebSocket debugger URL.  '
            'Ensure Chrome was started with --remote-debugging-port.')

    # Parse the URL: ws://host:port/path
    stripped = ws_debugger_url.removeprefix('ws://')
    host_port, _, path = stripped.partition('/')
    path = '/' + path
    ws_host, _, ws_port_str = host_port.partition(':')
    ws_port = int(ws_port_str) if ws_port_str else port

    ws = _WebSocketClient(ws_host, ws_port, path)
    ws.connect()
    try:
        cdp = _CDPSession(ws)
        cdp.send('Runtime.enable')

        snapshot_js = _js_snapshot_messages(response_selector)
        snapshot_result = cdp.send('Runtime.evaluate', {
            'expression': snapshot_js,
            'returnByValue': True,
        })
        existing_messages = set(_extract_value_list(snapshot_result))

        inject_js = _js_inject_and_send(message, input_selector)
        inject_result = cdp.send('Runtime.evaluate', {
            'expression': inject_js,
            'returnByValue': True,
            'awaitPromise': True,
        })

        result_obj = inject_result.get('result', {}).get('result', {})
        if result_obj.get('subtype') == 'error':
            desc = result_obj.get('description', 'unknown error')
            raise InjectionError(f'JavaScript injection failed: {desc}')

        # Poll for new messages.
        deadline = time.monotonic() + response_timeout
        while time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL_SECONDS)
            check_result = cdp.send('Runtime.evaluate', {
                'expression': snapshot_js,
                'returnByValue': True,
            })
            current_messages = _extract_value_list(check_result)
            new_messages = [m for m in current_messages
                            if m not in existing_messages and m.strip()]
            if new_messages:
                return new_messages[-1].strip()

        raise ResponseTimeoutError(
            f'No response received within {response_timeout:.0f} s.')
    finally:
        ws.close()


# ---------------------------------------------------------------------------
# Interactive mode.
# ---------------------------------------------------------------------------

def run_interactive(port: int,
                    tab_pattern: Optional[str],
                    input_selector: Optional[str],
                    response_selector: Optional[str],
                    response_timeout: float):
    """Start an interactive REPL-style chat session."""
    print(f'Connecting to Chrome on port {port}…')
    tabs = list_tabs(port)
    tab = find_tab(tabs, tab_pattern)
    if tab is None:
        msg = (f'No tab matching {tab_pattern!r}.'
               if tab_pattern else 'No suitable tab found.')
        print(f'Error: {msg}', file=sys.stderr)
        sys.exit(1)

    print(f'Tab: [{tab.get("title", "untitled")}]')
    print(f'URL: {tab.get("url", "")}')
    print('Type your message and press Enter.  '
          'Type "exit" or press Ctrl-C to quit.\n')

    while True:
        try:
            line = input('You: ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\nBye.')
            break

        if line.lower() in ('exit', 'quit', ''):
            break

        try:
            response = inject_message_and_wait(
                port=port,
                message=line,
                tab_pattern=tab_pattern,
                input_selector=input_selector,
                response_selector=response_selector,
                response_timeout=response_timeout,
            )
            print(f'Response: {response}')
        except BrowserChatError as exc:
            print(f'Error: {exc}', file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='browser_chat_cli',
        description='Chat via any open browser tab with a chat interface.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)

    parser.add_argument('--port',
                        type=int,
                        default=_DEFAULT_PORT,
                        metavar='PORT',
                        help=(f'Chrome remote debugging port '
                              f'(default: {_DEFAULT_PORT})'))
    parser.add_argument('--tab',
                        metavar='PATTERN',
                        help='URL or title substring to identify the target '
                        'tab (case-insensitive).  Use --list-tabs to see '
                        'available tabs.  If omitted, the first non-internal '
                        'page tab is used.')
    parser.add_argument('--message',
                        '-m',
                        metavar='TEXT',
                        help='Message to send (required unless '
                        '--interactive or --list-tabs is used).')
    parser.add_argument('--input-selector',
                        metavar='CSS',
                        help='CSS selector for the chat input element.')
    parser.add_argument('--response-selector',
                        metavar='CSS',
                        help='CSS selector for response message elements.')
    parser.add_argument('--timeout',
                        type=float,
                        default=_DEFAULT_RESPONSE_TIMEOUT,
                        metavar='SECONDS',
                        help=('Seconds to wait for a response '
                              f'(default: {_DEFAULT_RESPONSE_TIMEOUT}).'))
    parser.add_argument('--interactive',
                        '-i',
                        action='store_true',
                        help='Start an interactive (REPL) chat session.')
    parser.add_argument('--list-tabs',
                        action='store_true',
                        help='Print all open tabs and exit.')

    args = parser.parse_args(argv)

    if args.list_tabs:
        try:
            tabs = list_tabs(args.port)
        except ConnectionError as exc:
            print(f'Error: {exc}', file=sys.stderr)
            sys.exit(1)
        page_tabs = [t for t in tabs if t.get('type') == 'page']
        if not page_tabs:
            print('No open page tabs found.')
            return
        print(f'Open tabs on port {args.port}:')
        for i, tab in enumerate(page_tabs):
            title = tab.get('title', 'untitled')
            url = tab.get('url', '')
            print(f'  [{i}] {title!r}')
            print(f'       {url}')
        return

    if args.interactive:
        run_interactive(
            port=args.port,
            tab_pattern=args.tab,
            input_selector=args.input_selector,
            response_selector=args.response_selector,
            response_timeout=args.timeout,
        )
        return

    if not args.message:
        parser.error('--message is required (or use --interactive / '
                     '--list-tabs).')

    try:
        response = inject_message_and_wait(
            port=args.port,
            message=args.message,
            tab_pattern=args.tab,
            input_selector=args.input_selector,
            response_selector=args.response_selector,
            response_timeout=args.timeout,
        )
        print(response)
    except BrowserChatError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
