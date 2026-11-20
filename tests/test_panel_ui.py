from classmate.bot.keyboards import main_reply_keyboard, wizard_cancel_keyboard
from classmate.bot.panel_handlers import (
    _is_panel_input_text,
    _markup,
    _parent_for_form,
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


def test_wizard_keyboard_has_a_return_to_menu_control():
    labels = _labels(wizard_cancel_keyboard())
    assert labels == {"⬅️ بازگشت"}


def test_panel_input_does_not_consume_telegram_commands():
    assert _is_panel_input_text("ABC123")
    assert not _is_panel_input_text("/start")
    assert not _is_panel_input_text("/start payload")
    assert not _is_panel_input_text("   /start")
    assert not _is_panel_input_text(None)


def test_join_form_back_returns_directly_to_main_menu():
    assert _parent_for_form("join", None) == "ui:close"


def test_join_form_has_an_explicit_back_callback():
    markup = _markup([], "ui:cancel_input")
    back = markup.inline_keyboard[-1][0]
    assert back.text == "⬅️ بازگشت"
    assert back.callback_data == "ui:cancel_input"


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
