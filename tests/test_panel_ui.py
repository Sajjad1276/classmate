from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import ReplyKeyboardMarkup

from classmate.bot.keyboards import (
    group_menu_keyboard,
    main_reply_keyboard,
    wizard_cancel_keyboard,
)
from classmate.bot.group_handlers import router as group_router
from classmate.bot.panel_form_processing import _send_join_retry_message
from classmate.bot.panel_handlers import (
    _is_panel_input_text,
    _parent_for_form,
    main_button_handler,
    router as panel_router,
)


def _labels(markup) -> set[str]:
    return {
        button.text
        for row in markup.keyboard
        for button in row
    }


def test_regular_user_sees_only_student_navigation():
    labels = _labels(main_reply_keyboard())

    assert "🏫 کلاس‌های من" in labels
    assert "🔑 عضویت در کلاس" in labels
    assert "➕ ثبت اطلاعات" in labels
    assert "🛡 پنل مدیر کل" not in labels
    assert "🏢 مدیریت مدرسه" not in labels
    assert "⚙️ مدیریت کلاس" not in labels
    assert "🕓 صف بررسی" not in labels


def test_main_keyboard_has_no_temporary_back_or_cancel_button():
    labels = _labels(main_reply_keyboard())

    assert "⬅️ بازگشت" not in labels
    assert "❌ لغو عملیات" not in labels


def test_platform_admin_sees_platform_and_school_management():
    labels = _labels(
        main_reply_keyboard(
            is_platform_admin=True,
            is_school_admin=True,
        )
    )

    assert "🛡 پنل مدیر کل" in labels
    assert "🏢 مدیریت مدرسه" in labels


def test_class_manager_sees_class_management_but_not_platform_admin():
    labels = _labels(main_reply_keyboard(is_class_manager=True, is_approver=True))

    assert "⚙️ مدیریت کلاس" in labels
    assert "🕓 صف بررسی" in labels
    assert "🛡 پنل مدیر کل" not in labels
    assert "🏢 مدیریت مدرسه" not in labels


def test_wizard_keyboard_replaces_main_menu_with_only_a_reply_back_button():
    keyboard = wizard_cancel_keyboard()
    assert isinstance(keyboard, ReplyKeyboardMarkup)
    assert _labels(keyboard) == {"⬅️ بازگشت"}
    assert not getattr(keyboard, "inline_keyboard", None)


def test_panel_input_does_not_consume_telegram_commands():
    assert _is_panel_input_text("ABC123")
    assert not _is_panel_input_text("/start")
    assert not _is_panel_input_text("/start payload")
    assert not _is_panel_input_text("   /start")
    assert not _is_panel_input_text(None)


def test_join_form_back_returns_directly_to_main_menu():
    assert _parent_for_form("join", None) == "ui:close"


async def test_join_button_replaces_the_existing_panel_and_shows_reply_keyboard_back():
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"panel_message_id": 99}),
        clear=AsyncMock(),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
    )
    message = SimpleNamespace(
        text="🔑 عضویت در کلاس",
        chat=SimpleNamespace(id=123),
        delete=AsyncMock(),
    )
    prompt = SimpleNamespace(message_id=456)
    bot = SimpleNamespace(
        delete_message=AsyncMock(),
        send_message=AsyncMock(return_value=prompt),
    )

    await main_button_handler(message, state, bot)

    bot.delete_message.assert_awaited_once_with(123, 99)
    message.delete.assert_not_awaited()
    sent_args = bot.send_message.await_args
    assert sent_args.args[:2] == (
        123,
        "🔑 کد دعوت کلاس را وارد کن. این کد را از مدیر کلاس دریافت کن.",
    )
    assert isinstance(sent_args.kwargs["reply_markup"], ReplyKeyboardMarkup)
    assert _labels(sent_args.kwargs["reply_markup"]) == {"⬅️ بازگشت"}
    state.update_data.assert_awaited()
    assert state.update_data.await_args_list[-1].kwargs == {"panel_message_id": 456}


async def test_invalid_join_code_is_sent_as_a_separate_message():
    bot = SimpleNamespace(send_message=AsyncMock())

    await _send_join_retry_message(
        bot,
        123,
        ValueError("کد دعوت معتبر نیست یا اعتبار آن تمام شده است."),
    )

    bot.send_message.assert_awaited_once_with(
        123,
        "⚠️ کد دعوت معتبر نیست یا اعتبار آن تمام شده است.\n\nکد را دوباره بررسی و وارد کن.",
    )


def test_panel_router_registers_confirmed_cancellation_callbacks():
    callback_names = {
        handler.callback.__name__
        for handler in panel_router.observers["callback_query"].handlers
    }
    assert "cancelable_records_callback" in callback_names
    assert "cancel_record_confirm_callback" in callback_names
    assert "cancel_record_execute_callback" in callback_names


def test_panel_router_registers_school_group_management_callbacks():
    callback_names = {
        handler.callback.__name__
        for handler in panel_router.observers["callback_query"].handlers
    }
    assert "school_management_panel" in callback_names
    assert "school_group_link" in callback_names
    assert "school_group_reset_confirm" in callback_names
    assert "school_group_reset_do" in callback_names


def test_school_context_is_preserved_when_a_school_form_is_cancelled():
    assert _parent_for_form("year_new", None, 42) == "ui:school_manage:42"
    assert _parent_for_form("class_new", None, 9) == "ui:school_manage:9"


def test_panel_router_registers_school_class_selectors():
    callback_names = {
        handler.callback.__name__
        for handler in panel_router.observers["callback_query"].handlers
    }
    assert "school_class_manager_selector" in callback_names
    assert "school_classes_view" in callback_names


def test_group_menu_exposes_records_but_review_is_role_gated():
    regular = {
        button.callback_data
        for row in group_menu_keyboard(can_review=False).inline_keyboard
        for button in row
    }
    approver = {
        button.callback_data
        for row in group_menu_keyboard(can_review=True).inline_keyboard
        for button in row
    }
    assert {"group:homework", "group:exams", "group:week", "group:today"} <= regular
    assert "group:pending" not in regular
    assert "group:pending" in approver
    assert {"new:homework", "new:exam", "new:report"} <= regular


def test_group_router_registers_group_menu_callback():
    callback_names = {
        handler.callback.__name__
        for handler in group_router.observers["callback_query"].handlers
    }
    assert "group_menu_action" in callback_names
