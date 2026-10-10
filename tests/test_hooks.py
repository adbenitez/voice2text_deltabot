"""Unit tests for voice2text_deltabot.hooks."""

import dataclasses
import time
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from deltachat2 import (
    BasicChat,
    ChatType,
    EventTypeError,
    EventTypeInfo,
    EventTypeSecurejoinInviterProgress,
    EventTypeWarning,
    Message,
    MessageData,
    Viewtype,
)

from voice2text_deltabot import hooks

BLOB = "/tmp/blob"


def test_hook_attribute_names_still_exist_in_deltachat2():
    """Guard the deltachat2 2.x rename (CoreEvent/MessageViewtype/MsgData -> ...)."""
    assert {"id", "chat_id", "file", "is_bot", "text", "view_type"} <= {
        f.name for f in dataclasses.fields(Message)
    }
    assert "chat_type" in {f.name for f in dataclasses.fields(BasicChat)}
    assert {"text", "quoted_message_id"} <= {
        f.name for f in dataclasses.fields(MessageData)
    }
    assert hasattr(ChatType, "SINGLE")
    assert hasattr(Viewtype, "VOICE") and hasattr(Viewtype, "AUDIO")


def make_msg(**kwargs):
    """Build a stand-in for deltachat2.Message with just the used attributes."""
    attrs = {
        "id": 1,
        "chat_id": 10,
        "file": BLOB,
        "is_bot": False,
        "text": "hello",
        "view_type": Viewtype.TEXT,
    }
    attrs.update(kwargs)
    return SimpleNamespace(**attrs)


def make_bot(chat_type=ChatType.SINGLE):
    """Build a stand-in for deltachat2.Bot with a mocked rpc."""
    bot = SimpleNamespace()
    bot.rpc = MagicMock()
    bot.rpc.send_msg.return_value = 42
    bot.rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type=chat_type)
    bot.logger = MagicMock()
    return bot


def seg(end, text="привет", avg_logprob=-0.1, no_speech_prob=0.05):
    """Build a stand-in for a faster-whisper segment."""
    return SimpleNamespace(
        end=end, text=text, avg_logprob=avg_logprob, no_speech_prob=no_speech_prob
    )


class FakeModel:
    """Stand-in for faster_whisper.WhisperModel."""

    def __init__(self, segments, duration=5.0, language="ru", probability=0.99):
        self.segments = segments
        self.info = SimpleNamespace(
            duration=duration, language=language, language_probability=probability
        )
        self.transcribed = []

    def transcribe(self, path):
        self.transcribed.append(path)
        return self.segments, self.info


@pytest.fixture(autouse=True)
def isolate_module_state():
    """Keep model/ARGS changes out of other tests."""
    old = hooks.MODEL, hooks.ARGS, hooks.LOGGER, hooks.MODEL_USED_AT
    hooks.MODEL = FakeModel([seg(1.0, "test")])
    hooks.ARGS = Namespace(
        max_duration=600,
        model="small",
        compute_type="int8",
        cpu_threads=0,
        model_idle_timeout=30,
    )
    hooks.LOGGER = MagicMock()
    hooks.MODEL_USED_AT = 0.0
    yield
    hooks.MODEL, hooks.ARGS, hooks.LOGGER, hooks.MODEL_USED_AT = old
    if hooks.MODEL_LOCK.locked():  # a test left it held
        hooks.MODEL_LOCK.release()


def newmsg_event(msg):
    return SimpleNamespace(msg=msg)


# --- on_newmsg -----------------------------------------------------------------


def test_single_chat_text_message_gets_help():
    bot = make_bot(ChatType.SINGLE)
    msg = make_msg(text="привет")
    hooks.on_newmsg(bot, 1, newmsg_event(msg))

    bot.rpc.markseen_msgs.assert_called_once_with(1, [msg.id])
    bot.rpc.send_msg.assert_called_once()
    accid, chat_id, data = bot.rpc.send_msg.call_args.args
    assert (accid, chat_id) == (1, msg.chat_id)
    assert isinstance(data, MessageData)
    assert data.text == hooks.HELP
    assert bot.rpc.send_edit_request.call_count == 0


def test_group_text_message_is_ignored():
    bot = make_bot(ChatType.GROUP)
    hooks.on_newmsg(bot, 1, newmsg_event(make_msg(text="привет")))

    bot.rpc.markseen_msgs.assert_not_called()
    bot.rpc.send_msg.assert_not_called()


def test_single_chat_message_from_bot_is_seen_but_not_answered():
    bot = make_bot(ChatType.SINGLE)
    msg = make_msg(text="я бот", is_bot=True)
    hooks.on_newmsg(bot, 1, newmsg_event(msg))

    bot.rpc.markseen_msgs.assert_called_once_with(1, [msg.id])
    bot.rpc.send_msg.assert_not_called()


def test_voice_message_is_transcribed():
    hooks.MODEL = FakeModel([seg(1.0, "привет"), seg(2.0, "как дела")], duration=3.0)
    bot = make_bot()

    hooks.on_newmsg(bot, 1, newmsg_event(make_msg(view_type=Viewtype.VOICE)))

    assert hooks.MODEL.transcribed == [BLOB]
    _, _, placeholder = bot.rpc.send_msg.call_args.args
    assert placeholder.text == "⏳"
    assert placeholder.quoted_message_id == 1
    bot.rpc.send_edit_request.assert_called_once_with(1, 42, "привет\nкак дела")


def test_audio_viewtype_is_transcribed_too():
    hooks.MODEL = FakeModel([seg(1.0, "текст")])
    bot = make_bot()

    hooks.on_newmsg(bot, 1, newmsg_event(make_msg(view_type=Viewtype.AUDIO)))

    assert hooks.MODEL.transcribed == [BLOB]
    bot.rpc.send_edit_request.assert_called_once_with(1, 42, "текст")


def test_voice_in_group_is_transcribed():
    bot = make_bot(ChatType.GROUP)
    hooks.on_newmsg(bot, 1, newmsg_event(make_msg(view_type=Viewtype.VOICE)))

    bot.rpc.markseen_msgs.assert_not_called()
    bot.rpc.send_edit_request.assert_called_once_with(1, 42, "test")


# --- _process_audio ------------------------------------------------------------


def test_noisy_segments_are_dropped():
    hooks.MODEL = FakeModel(
        [
            seg(1.0, "нормально"),
            seg(2.0, "мусор по вероятности", avg_logprob=-0.9),
            seg(3.0, "тишина", no_speech_prob=0.9),
            seg(4.0, "..."),
            seg(5.0, "   "),
            seg(6.0, "тоже нормально"),
        ],
        duration=7.0,
    )
    bot = make_bot()

    hooks._process_audio(bot, 1, make_msg(view_type=Viewtype.VOICE))

    bot.rpc.send_edit_request.assert_called_once_with(
        1, 42, "нормально\nтоже нормально"
    )


def test_empty_transcription_deletes_placeholder_and_reacts():
    hooks.MODEL = FakeModel([seg(1.0, "...", avg_logprob=-1.0)], duration=2.0)
    bot = make_bot()
    msg = make_msg(view_type=Viewtype.VOICE)

    hooks._process_audio(bot, 1, msg)

    bot.rpc.delete_messages_for_all.assert_called_once_with(1, [42])
    bot.rpc.send_reaction.assert_called_once_with(1, msg.id, ["🎶"])
    bot.rpc.send_edit_request.assert_not_called()


def test_max_duration_stops_and_marks_truncation():
    hooks.ARGS = Namespace(max_duration=30)
    hooks.MODEL = FakeModel([seg(10.0, "первое"), seg(40.0, "второе")], duration=90.0)
    bot = make_bot()

    hooks._process_audio(bot, 1, make_msg(view_type=Viewtype.VOICE))

    bot.rpc.send_edit_request.assert_called_once_with(1, 42, "первое\n[...]")


def test_long_transcription_sends_progress_edits():
    hooks.MODEL = FakeModel([seg(30.0, "раз"), seg(90.0, "два")], duration=100.0)
    bot = make_bot()

    hooks._process_audio(bot, 1, make_msg(view_type=Viewtype.VOICE))

    assert bot.rpc.send_edit_request.call_args_list == [
        call(1, 42, "раз\n\n⏳"),
        call(1, 42, "раз\nдва"),
    ]


# --- other hooks ---------------------------------------------------------------


def test_delete_msgs_post_hook_deletes_the_message():
    bot = make_bot()
    hooks.delete_msgs(bot, 1, newmsg_event(make_msg(id=7)))

    bot.rpc.delete_messages.assert_called_once_with(1, [7])


@pytest.mark.parametrize(
    "event,method",
    [
        (EventTypeInfo(msg="info-msg"), "debug"),
        (EventTypeWarning(msg="warning-msg"), "warning"),
        (EventTypeError(msg="error-msg"), "error"),
    ],
)
def test_raw_event_is_logged_with_the_matching_level(event, method):
    bot = make_bot()

    hooks._log_event(bot, 1, event)

    getattr(bot.logger, method).assert_called_once_with(event.msg)


def test_securejoin_progress_sends_help_to_humans():
    bot = make_bot()
    bot.rpc.get_contact.return_value = SimpleNamespace(is_bot=False)
    bot.rpc.create_chat_by_contact_id.return_value = 55
    event = EventTypeSecurejoinInviterProgress(
        chat_id=0, chat_type=ChatType.SINGLE, contact_id=7, progress=1000
    )

    hooks._log_event(bot, 1, event)

    bot.rpc.create_chat_by_contact_id.assert_called_once_with(1, 7)
    bot.rpc.send_msg.assert_called_once()
    accid, chat_id, data = bot.rpc.send_msg.call_args.args
    assert (accid, chat_id) == (1, 55)
    assert data.text == hooks.HELP


def test_securejoin_progress_skips_bots():
    bot = make_bot()
    bot.rpc.get_contact.return_value = SimpleNamespace(is_bot=True)
    event = EventTypeSecurejoinInviterProgress(
        chat_id=0, chat_type=ChatType.SINGLE, contact_id=7, progress=1000
    )

    hooks._log_event(bot, 1, event)

    bot.rpc.create_chat_by_contact_id.assert_not_called()
    bot.rpc.send_msg.assert_not_called()


def test_securejoin_incomplete_progress_is_ignored():
    bot = make_bot()
    bot.rpc.get_contact.return_value = SimpleNamespace(is_bot=False)
    event = EventTypeSecurejoinInviterProgress(
        chat_id=0, chat_type=ChatType.SINGLE, contact_id=7, progress=500
    )

    hooks._log_event(bot, 1, event)

    bot.rpc.get_contact.assert_not_called()
    bot.rpc.send_msg.assert_not_called()


# --- CPU threads ----------------------------------------------------------------

PROC_CPUINFO = """processor\t: 0
physical id\t: 0
core id\t\t: 0

processor\t: 1
physical id\t: 0
core id\t\t: 1

processor\t: 2
physical id\t: 1
core id\t\t: 0

processor\t: 3
physical id\t: 1
core id\t\t: 1
"""


def test_physical_cpu_count_counts_distinct_core_ids(tmp_path):
    path = tmp_path / "cpuinfo"
    path.write_text(PROC_CPUINFO, encoding="utf-8")

    assert hooks.physical_cpu_count(str(path)) == 4


def test_physical_cpu_count_without_core_id(tmp_path):
    """VMs often expose no core id at all; that must not be read as one core."""
    path = tmp_path / "cpuinfo"
    path.write_text(
        "processor\t: 0\nmodel name\t: Intel(R) Xeon(R)\n", encoding="utf-8"
    )

    assert hooks.physical_cpu_count(str(path)) == 0


def test_physical_cpu_count_missing_file():
    assert hooks.physical_cpu_count("/nonexistent/cpuinfo") == 0


def test_resolve_cpu_threads_prefers_the_flag():
    assert hooks.resolve_cpu_threads(12) == 12


def test_resolve_cpu_threads_uses_every_usable_cpu(monkeypatch):
    monkeypatch.setattr(hooks.os, "process_cpu_count", lambda: 12)

    assert hooks.resolve_cpu_threads(0) == 12


def test_resolve_cpu_threads_falls_back_to_cpu_count(monkeypatch):
    monkeypatch.setattr(hooks.os, "process_cpu_count", lambda: None)
    monkeypatch.setattr(hooks.os, "cpu_count", lambda: 5)

    assert hooks.resolve_cpu_threads(0) == 5


def whisper_factory(monkeypatch, calls, segments=None):
    """Patch WhisperModel with a recorder that still transcribes."""

    class FakeWhisperModel:
        def __init__(self, model, **kwargs):
            calls.append({"model": model, **kwargs})
            self._model = FakeModel(segments or [seg(1.0, "загружено лениво")])

        def transcribe(self, path):
            return self._model.transcribe(path)

    monkeypatch.setattr(hooks, "WhisperModel", FakeWhisperModel)


def on_start_args(**kwargs):
    args = {
        "model": "small",
        "compute_type": "int8",
        "cpu_threads": 0,
        "model_idle_timeout": 30,
        "no_auto_unload": False,
        "max_duration": 600,
    }
    args.update(kwargs)
    return Namespace(**args)


def test_on_start_does_not_load_the_model(monkeypatch):
    calls = []
    whisper_factory(monkeypatch, calls)
    hooks.MODEL = None
    bot = make_bot()

    hooks.on_start(bot, on_start_args(model="large", model_idle_timeout=0))

    assert calls == []
    assert hooks.MODEL is None
    bot.logger.info.assert_called_once()
    message, *rest = bot.logger.info.call_args.args
    assert "cpu_threads" in message
    assert "kept in memory" in rest


def test_on_start_starts_the_idle_watchdog(monkeypatch):
    started = []
    monkeypatch.setattr(hooks, "WhisperModel", lambda *a, **kw: None)
    monkeypatch.setattr(
        hooks.threading,
        "Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs)),
    )

    hooks.on_start(make_bot(), on_start_args(model_idle_timeout=0))
    assert started == []

    hooks.on_start(make_bot(), on_start_args(model_idle_timeout=30))
    assert len(started) == 1
    assert started[0]["args"] == (30,)
    assert started[0]["daemon"] is True


def test_no_auto_unload_keeps_the_model(monkeypatch):
    started = []
    monkeypatch.setattr(hooks, "WhisperModel", lambda *a, **kw: None)
    monkeypatch.setattr(
        hooks.threading,
        "Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs)),
    )
    bot = make_bot()

    hooks.on_start(bot, on_start_args(model_idle_timeout=30, no_auto_unload=True))

    assert started == []  # no watchdog, whatever the timeout says
    assert "kept in memory" in bot.logger.info.call_args.args


def test_model_is_loaded_on_first_use_and_reused(monkeypatch):
    calls = []
    whisper_factory(monkeypatch, calls)
    hooks.MODEL = None
    hooks.ARGS = on_start_args(model="large", cpu_threads=3)

    with hooks.model_in_use() as model:
        assert model is not None
    assert calls == [
        {"model": "large", "device": "auto", "compute_type": "int8", "cpu_threads": 3}
    ]

    loaded = hooks.MODEL
    with hooks.model_in_use() as again:
        assert again is loaded
    assert len(calls) == 1  # not loaded a second time


def test_model_is_loaded_when_a_message_needs_it(monkeypatch):
    calls = []
    whisper_factory(monkeypatch, calls, [seg(1.0, "ленивая загрузка")])
    hooks.MODEL = None
    bot = make_bot()

    hooks._process_audio(bot, 1, make_msg(view_type=Viewtype.VOICE))

    assert len(calls) == 1
    bot.rpc.send_edit_request.assert_called_once_with(1, 42, "ленивая загрузка")


def test_process_audio_marks_the_model_as_used():
    hooks.MODEL_USED_AT = 0.0
    bot = make_bot()

    hooks._process_audio(bot, 1, make_msg(view_type=Viewtype.VOICE))

    assert hooks.MODEL_USED_AT > 0


def test_idle_model_is_freed():
    hooks.MODEL = object()
    hooks.MODEL_USED_AT = time.monotonic() - 31

    assert hooks.unload_if_idle(30) is True
    assert hooks.MODEL is None


def test_fresh_model_is_kept():
    hooks.MODEL = object()
    hooks.MODEL_USED_AT = time.monotonic()

    assert hooks.unload_if_idle(30) is False
    assert hooks.MODEL is not None


def test_model_is_kept_while_it_is_transcribing():
    hooks.MODEL = object()
    hooks.MODEL_USED_AT = time.monotonic() - 31
    assert hooks.MODEL_LOCK.acquire(blocking=False)
    try:
        assert hooks.unload_if_idle(30) is False
    finally:
        hooks.MODEL_LOCK.release()
    assert hooks.MODEL is not None


def test_nothing_to_unload_without_a_model():
    hooks.MODEL = None

    assert hooks.unload_if_idle(30) is False
