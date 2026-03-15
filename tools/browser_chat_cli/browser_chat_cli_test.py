#!/usr/bin/env python3
# Copyright 2025 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Unit tests for browser_chat_cli."""

import json
import sys
import threading
import unittest
from io import StringIO
from unittest import mock

# Import the module under test.
import browser_chat_cli as cli


class ListTabsTest(unittest.TestCase):
    """Tests for list_tabs()."""

    def test_returns_parsed_json_on_success(self):
        payload = [{'id': '1', 'type': 'page', 'url': 'https://example.com'}]
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch('urllib.request.urlopen', return_value=mock_resp):
            result = cli.list_tabs(9222)

        self.assertEqual(result, payload)

    def test_raises_connection_error_on_failure(self):
        with mock.patch('urllib.request.urlopen',
                        side_effect=OSError('refused')):
            with self.assertRaises(cli.ConnectionError) as ctx:
                cli.list_tabs(9222)
        self.assertIn('9222', str(ctx.exception))


class FindTabTest(unittest.TestCase):
    """Tests for find_tab()."""

    def _make_tab(self, url, title='', tab_type='page'):
        return {
            'type': tab_type,
            'url': url,
            'title': title,
            'webSocketDebuggerUrl': f'ws://localhost:9222/devtools/page/abc',
        }

    def test_pattern_matches_url(self):
        tabs = [
            self._make_tab('https://web.telegram.org/k/'),
            self._make_tab('https://web.whatsapp.com/'),
        ]
        result = cli.find_tab(tabs, 'whatsapp')
        self.assertIsNotNone(result)
        self.assertIn('whatsapp', result['url'])

    def test_pattern_matches_title(self):
        tabs = [
            self._make_tab('https://example.com/', title='WhatsApp'),
        ]
        result = cli.find_tab(tabs, 'whatsapp')
        self.assertIsNotNone(result)
        self.assertEqual(result['title'], 'WhatsApp')

    def test_pattern_not_found_returns_none(self):
        tabs = [self._make_tab('https://example.com/')]
        result = cli.find_tab(tabs, 'nonexistent-pattern-xyz')
        self.assertIsNone(result)

    def test_auto_detect_known_messenger(self):
        tabs = [
            self._make_tab('https://news.ycombinator.com/'),
            self._make_tab('https://web.telegram.org/k/', title='Telegram'),
        ]
        result = cli.find_tab(tabs)
        self.assertIsNotNone(result)
        self.assertIn('telegram', result['url'])

    def test_auto_detect_falls_back_to_first_page(self):
        tabs = [
            self._make_tab('https://example.com/', title='Example'),
        ]
        result = cli.find_tab(tabs)
        self.assertIsNotNone(result)
        self.assertEqual(result['url'], 'https://example.com/')

    def test_non_page_tabs_are_ignored(self):
        tabs = [
            {'type': 'background_page', 'url': 'https://example.com/bg'},
            self._make_tab('https://example.com/page'),
        ]
        result = cli.find_tab(tabs)
        self.assertEqual(result['url'], 'https://example.com/page')

    def test_empty_list_returns_none(self):
        self.assertIsNone(cli.find_tab([]))


class ExtractValueListTest(unittest.TestCase):
    """Tests for _extract_value_list()."""

    def test_extracts_plain_list(self):
        result = {'result': {'result': {'type': 'object', 'value': ['a', 'b']}}}
        self.assertEqual(cli._extract_value_list(result), ['a', 'b'])

    def test_skips_empty_strings(self):
        result = {'result': {'result': {'type': 'object', 'value': ['', 'x']}}}
        self.assertEqual(cli._extract_value_list(result), ['x'])

    def test_returns_empty_on_missing_value(self):
        result = {'result': {'result': {'type': 'undefined'}}}
        self.assertEqual(cli._extract_value_list(result), [])

    def test_returns_empty_on_bad_structure(self):
        self.assertEqual(cli._extract_value_list({}), [])
        self.assertEqual(cli._extract_value_list({'result': None}), [])


class JsSnapshotTest(unittest.TestCase):
    """Tests for _js_snapshot_messages()."""

    def test_custom_selector_is_embedded(self):
        js = cli._js_snapshot_messages('.my-msg')
        self.assertIn('.my-msg', js)
        self.assertIn('querySelectorAll', js)

    def test_default_selector_is_heuristic(self):
        js = cli._js_snapshot_messages(None)
        # Should contain the generic heuristic logic.
        self.assertIn('candidates', js)
        self.assertIn('querySelectorAll', js)


class JsInjectAndSendTest(unittest.TestCase):
    """Tests for _js_inject_and_send()."""

    def test_message_is_embedded(self):
        js = cli._js_inject_and_send('Hello World')
        self.assertIn('Hello World', js)

    def test_custom_selector_is_embedded(self):
        js = cli._js_inject_and_send('Hi', '#my-input')
        self.assertIn('#my-input', js)
        # Should not contain the fallback auto-detection logic.
        self.assertNotIn('candidates', js)

    def test_default_uses_candidate_list(self):
        js = cli._js_inject_and_send('Hi')
        self.assertIn('candidates', js)

    def test_special_chars_are_escaped(self):
        # json.dumps must escape the double-quote inside the message so that the
        # embedded JavaScript string literal remains syntactically valid.
        msg = 'He said "hello" and it\'s fine'
        js = cli._js_inject_and_send(msg)
        # The raw double-quote character must not appear un-escaped inside the
        # JavaScript string: json.dumps produces \" for " inside a string.
        self.assertIn('\\"hello\\"', js)
        # The apostrophe/single-quote is safe inside a JSON double-quoted string
        # and should appear literally.
        self.assertIn("it's fine", js)

    def test_enter_key_is_dispatched(self):
        js = cli._js_inject_and_send('msg')
        self.assertIn('Enter', js)
        self.assertIn('KeyboardEvent', js)


class MainCLITest(unittest.TestCase):
    """Tests for the argparse CLI layer (main())."""

    def _run_main(self, args):
        """Run main() with given args list, capturing stdout/stderr."""
        out, err = StringIO(), StringIO()
        with mock.patch('sys.stdout', out), mock.patch('sys.stderr', err):
            try:
                cli.main(args)
                exit_code = 0
            except SystemExit as exc:
                exit_code = exc.code
        return exit_code, out.getvalue(), err.getvalue()

    def test_list_tabs_output(self):
        tabs = [{
            'type': 'page',
            'url': 'https://web.whatsapp.com/',
            'title': 'WhatsApp',
        }]
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps(tabs).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch('urllib.request.urlopen', return_value=mock_resp):
            code, out, _ = self._run_main(['--list-tabs'])

        self.assertEqual(code, 0)
        self.assertIn('WhatsApp', out)
        self.assertIn('web.whatsapp.com', out)

    def test_missing_message_exits_nonzero(self):
        code, _, err = self._run_main([])
        self.assertNotEqual(code, 0)

    def test_message_flag_calls_inject(self):
        with mock.patch.object(cli,
                               'inject_message_and_wait',
                               return_value='Hi back!') as mock_inject:
            code, out, _ = self._run_main(['--message', 'Hello', '--port', '9222'])

        self.assertEqual(code, 0)
        self.assertIn('Hi back!', out)
        mock_inject.assert_called_once()
        call_kwargs = mock_inject.call_args.kwargs
        self.assertEqual(call_kwargs['message'], 'Hello')
        self.assertEqual(call_kwargs['port'], 9222)

    def test_connection_error_exits_nonzero(self):
        with mock.patch.object(cli,
                               'inject_message_and_wait',
                               side_effect=cli.ConnectionError('refused')):
            code, _, err = self._run_main(['--message', 'Hi'])

        self.assertNotEqual(code, 0)
        self.assertIn('refused', err)

    def test_response_timeout_exits_nonzero(self):
        with mock.patch.object(cli,
                               'inject_message_and_wait',
                               side_effect=cli.ResponseTimeoutError('timed out')):
            code, _, err = self._run_main(['--message', 'Hi'])

        self.assertNotEqual(code, 0)
        self.assertIn('timed out', err)


class WebSocketClientHandshakeTest(unittest.TestCase):
    """Tests for _WebSocketClient handshake parsing."""

    def _make_client(self):
        return cli._WebSocketClient('localhost', 9222, '/devtools/page/abc')

    def test_send_lock_exists(self):
        client = self._make_client()
        self.assertIsInstance(client._send_lock, type(threading.Lock()))

    def test_recv_buf_initially_empty(self):
        client = self._make_client()
        self.assertEqual(client._recv_buf, b'')

    def test_close_when_not_connected_is_safe(self):
        client = self._make_client()
        # Should not raise even if never connected.
        client.close()


if __name__ == '__main__':
    unittest.main()
