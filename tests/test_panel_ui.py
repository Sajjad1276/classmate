from classmate.bot.keyboards import main_reply_keyboard, wizard_cancel_keyboard


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
