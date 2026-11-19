from classmate.bot.panel_handlers import _should_create_panel_message
from classmate.bot.keyboards import (
    group_menu_keyboard,
    main_reply_keyboard,
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
