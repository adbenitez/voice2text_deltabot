# Voice To Text Bot

[![Latest Release](https://img.shields.io/pypi/v/voice2text-deltabot.svg)](https://pypi.org/project/voice2text-deltabot)
[![CI](https://github.com/deltachat-bot/voice2text_deltabot/actions/workflows/python-ci.yml/badge.svg)](https://github.com/deltachat-bot/voice2text_deltabot/actions/workflows/python-ci.yml)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

A voice-to-text converter bot for Delta Chat.

## Install

```sh
pip install voice2text-deltabot
```

The bot uses [Faster Whisper](https://github.com/guillaumekln/faster-whisper/) to extract the text
from voice messages.

## Usage

To configure the bot:

```sh
voice2text-bot init bot@example.org SuperHardPassword
```

**(Optional)** To customize the bot name, avatar and status/signature:

```sh
voice2text-bot config selfavatar "/path/to/avatar.png"
voice2text-bot config displayname "Voice To Text"
voice2text-bot config selfstatus "Hi, send me some voice message to convert it to text"
```

Finally you can start the bot with:

```sh
voice2text-bot serve
```

To see the available options, run in the command line:

```
voice2text-bot --help
```

## Run as a systemd user service

`contrib/install-user-service.sh` does the whole setup, no root needed:

```sh
git clone https://github.com/adbenitez/voice2text_deltabot
cd voice2text_deltabot
./contrib/install-user-service.sh --chatmail cm1.wwire.su
```

If the config folder has no account while accounts exist in another known location,
it prints those paths and refuses to create a second one (pass `--config-dir` to use
the existing account, or `--chatmail`/`--email` to deliberately create one).

It runs `uv sync`, creates the account (`DCACCOUNT:<host>`, so no address or
password is needed on a chatmail relay), renders `contrib/voice2text-bot.service.in`
into `~/.config/systemd/user/`, enables and starts the service, and prints the
invitation link to scan in Delta Chat. `--dry-run` prints what it would do and
`--help` lists the options (`--email`/`--password`, `--model`, `--name`,
`--config-dir`).

Notes for running it on another machine:

- **Python 3.13+** is required (`uv` can fetch it itself), and the only thing the
  machine needs is `uv`; everything else lives in the project venv.
- the Delta Chat core comes from that venv on purpose — do not install a
  separately managed core, see the note in `pyproject.toml`;
- the first start downloads the whisper model and the bot only starts answering
  once it is loaded. `large` (the default) is ~2.9 GB on disk and needs roughly
  4 GB of RAM; on a weak CPU `--model small` is the sane choice and `medium` is a
  reasonable compromise;
- the model is loaded on the first voice message and freed after
  `--model-idle-timeout` seconds without one (default 30; `0`, or the explicit
  `--no-auto-unload`, keeps it loaded).
  Measured with `large`/int8 on a 2-core box: loading costs 26-77 s and 3.2 GB RSS,
  and unloading does give the memory back (3.2 GB -> 85 MB), so the timeout is a
  trade-off between idle memory and the wait on the next message - 30 s suits
  `small`, for `large` something like 300 is friendlier;
- the model is given every CPU the process may use, because CTranslate2 caps its
  own default at 4 threads - on a bigger machine pass `--cpu-threads N`. What was
  measured (the chosen number, plus the logical and physical counts) is logged at
  startup; more threads than physical cores rarely helps, so the flag is also the
  way to cap it;
- logs: `journalctl --user -u voice2text-bot.service -f`, stop:
  `systemctl --user disable --now voice2text-bot.service`;
- to keep the bot running without an active login session:
  `loginctl enable-linger $USER`.

### Without systemd (uv tool)

`uv tool install .` on its own is not enough: `uv` exposes only the
`voice2text-bot` entry point, while `deltachat2` starts the core
(`deltachat-rpc-server`) by name from `PATH`, so the bot dies with
`FileNotFoundError: [Errno 2] No such file or directory: 'deltachat-rpc-server'`.
Ask uv to expose the core as well:

```sh
uv tool install . --with-executables-from deltachat-rpc-server
voice2text-bot init DCACCOUNT:cm1.wwire.su
voice2text-bot serve
```

Both this and `contrib/install-user-service.sh` use the config folder the CLI itself
picks (`~/.config/voice2text-bot` on Linux), so an account created one way is found
by the other instead of a second one being created next to it.

Note that the tool environment is separate from the project venv, so the core
version there is whatever `deltachat2[full]` pins, same as everywhere else.

Do **not** keep a second `deltachat-rpc-server` tool on the same `PATH`: the bot
starts the core by name, so a newer core installed separately (e.g.
`uv tool install deltachat-rpc-server` for other purposes) wins, and then every
event fails with

```
dacite.exceptions.MissingValueError: missing value for field "is_verified"
```

Core 2.62 replaced that field with `freshness`, while the bindings (2.58.0, newest
on PyPI) still require it. Check with `which -a deltachat-rpc-server` and
`deltachat-rpc-server --version` - the bot needs the 2.58.0 binary that ships in
its own environment.
