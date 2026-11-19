from classmate.bot.panel_handlers import (
    _should_create_panel_message,
    _should_process_panel_text,
)
from classmate.bot.keyboards import (
    group_menu_keyboard,
    main_reply_keyboard,
    reminder_class_picker,
    reminder_preferences_keyboard,
    wizard_cancel_keyboard,
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



def test_group_menu_shows_public_class_actions():
    rows = group_menu_keyboard().inline_keyboard
    labels = {button.text for row in rows for button in row}

    assert "📚 تکالیف" in labels
    assert "📝 امتحانات" in labels
    assert "📅 برنامه هفتگی" in labels
    assert "🗒 امروز" in labels
    assert "➕ ثبت تکلیف" in labels
    assert "🕓 صف بررسی" not in labels


def test_group_menu_shows_review_only_for_approvers():
    labels = {
        button.text
        for row in group_menu_keyboard(can_review=True).inline_keyboard
        for button in row
    }

    assert "🕓 صف بررسی" in labels



def test_panel_recreated_when_reply_keyboard_is_visible():
    assert _should_create_panel_message(123, True)


def test_panel_message_edited_when_reply_keyboard_is_hidden():
    assert not _should_create_panel_message(123, False)


def test_panel_created_when_no_previous_message_exists():
    assert _should_create_panel_message(None, False)



def test_reminder_class_picker_uses_class_scoped_callback_data():
    from types import SimpleNamespace

    markup = reminder_class_picker([SimpleNamespace(id=37, name="دهم تجربی")])
    button = markup.inline_keyboard[0][0]

    assert button.text == "🏫 دهم تجربی"
    assert button.callback_data == "reminders:class:37"


def test_reminder_preferences_keyboard_exposes_current_state_and_next_value():
    markup = reminder_preferences_keyboard(
        37, homework_enabled=True, exams_enabled=False
    )
    buttons = [button for row in markup.inline_keyboard for button in row]

    assert buttons[0].text == "🟢 تکالیف: روشن"
    assert buttons[0].callback_data == "reminders:set:homework:37:0"
    assert buttons[1].text == "⚫ امتحان‌ها: خاموش"
    assert buttons[1].callback_data == "reminders:set:exams:37:1"
    assert buttons[2].callback_data == "reminders:list"



def test_pending_input_does_not_consume_commands_or_main_menu_taps():
    assert not _should_process_panel_text("/start")
    assert not _should_process_panel_text("🏫 کلاس‌های من")
    assert not _should_process_panel_text("⬅️ بازگشت")
    assert _should_process_panel_text("نام درس")
    assert _should_process_panel_text("۱۴۰۵-۱۴۰۶")
