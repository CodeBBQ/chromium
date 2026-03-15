# `//tools/browser_chat_cli`

## Overview

`browser_chat_cli` is a command-line tool for interacting with web-based
messenger tabs in an already-running Chrome browser.  It connects to Chrome's
[Chrome DevTools Protocol (CDP)][cdp] endpoint, injects a text message into
the chat input field of the target tab, sends it, and returns the reply once
it appears in the conversation.

This makes it possible to chat from the terminal without leaving the browser
session open in the foreground.

---

## Prerequisites

Start Chrome (or Chromium) with remote debugging enabled:

```sh
chrome --remote-debugging-port=9222
# If Chrome complains about an existing profile, add a dedicated directory:
google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug
```

Log into your messenger in the browser as usual.

---

## Installation / Dependencies

The tool requires **Python 3.9+** and uses only the Python standard library –
no additional packages need to be installed.

---

## Usage

```
python3 tools/browser_chat_cli/browser_chat_cli.py [OPTIONS]
```

### Send a single message and print the response

```sh
python3 tools/browser_chat_cli/browser_chat_cli.py \
    --message "Hello!" \
    --tab "web.whatsapp.com"
```

### Interactive (REPL) chat mode

```sh
python3 tools/browser_chat_cli/browser_chat_cli.py \
    --interactive \
    --tab "web.telegram.org"
```

In this mode you type messages at the `You:` prompt and the tool prints the
bot/contact reply after each message.  Type `exit` or press `Ctrl-C` to quit.

### List all open tabs

```sh
python3 tools/browser_chat_cli/browser_chat_cli.py --list-tabs
```

### Full options

| Flag | Default | Description |
|------|---------|-------------|
| `--port PORT` | `9222` | Chrome remote debugging port |
| `--tab PATTERN` | auto | URL or title substring to pick the tab |
| `--message TEXT` / `-m TEXT` | — | Message to send (required in single-shot mode) |
| `--input-selector CSS` | auto | CSS selector for the chat input field |
| `--response-selector CSS` | auto | CSS selector for response message elements |
| `--timeout SECONDS` | `30` | How long to wait for a reply |
| `--interactive` / `-i` | — | Run in REPL mode |
| `--list-tabs` | — | Print all open tabs and exit |

---

## How it works

1. **Tab discovery** – The tool queries `http://localhost:<PORT>/json` to list
   all open browser tabs.
2. **Tab selection** – The correct tab is found by matching `--tab` against
   each tab's URL and title (case-insensitive substring match).  If `--tab`
   is omitted, the tool auto-detects common messenger sites (WhatsApp Web,
   Telegram Web, Messenger, Discord, Slack, …).
3. **WebSocket CDP session** – A raw WebSocket connection is opened to the
   tab's `webSocketDebuggerUrl`.
4. **Snapshot** – The current chat messages are snapshotted before injection,
   so only genuinely new messages are reported as the reply.
5. **Injection** – JavaScript is evaluated in the tab via `Runtime.evaluate`:
   - The chat input field is located (using the `--input-selector` CSS
     selector or a list of well-known heuristics).
   - Text is inserted using `document.execCommand('insertText', …)` for
     `contenteditable` fields (WhatsApp, Telegram, …) or the native value
     setter + `input` event for `<input>` / `<textarea>` elements.
   - An `Enter` `KeyboardEvent` is dispatched and any visible "Send" button
     is clicked.
6. **Response detection** – The tool polls the tab every 250 ms, re-evaluating
   the message snapshot expression.  The first new non-empty message text is
   returned as the reply.

---

## Supported messengers

The heuristics work out of the box with:

- **WhatsApp Web** (`web.whatsapp.com`)
- **Telegram Web** (`web.telegram.org`)
- **Facebook Messenger** (`messenger.com`)
- **Discord** (`discord.com`)
- **Slack** (`slack.com`)
- **Microsoft Teams** (`teams.microsoft.com`)
- **Google Chat** (`chat.google.com`)
- **Element / Matrix** (`element.io`, `matrix.to`)

For any other messenger, provide explicit `--input-selector` and
`--response-selector` CSS selectors.

---

## Running the tests

```sh
python3 tools/browser_chat_cli/browser_chat_cli_test.py
```

---

[cdp]: https://chromedevtools.github.io/devtools-protocol/
