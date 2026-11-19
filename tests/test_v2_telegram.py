from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from classmate.presentation.telegram.keyboards import (
    class_dashboard,
    main_keyboard,
    record_types,
    reminder_preferences,
)


def _callbacks(markup: InlineKeyboardMarkup) -> set[str]:
    return {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    }


def _labels(markup: InlineKeyboardMarkup) -> set[str]:
    return {
        button.text
        for row in markup.inline_keyboard
        for button in row
    }


def test_v2_main_dashboard_is_inline_not_persistent_reply_keyboard():
    markup = main_keyboard(
        platform_admin=True,
        school_admin=True,
        class_manager=True,
        approver=True,
    )

    assert isinstance(markup, InlineKeyboardMarkup)
    assert not isinstance(markup, ReplyKeyboardMarkup)
    assert "v2:classes:open" in _callbacks(markup)
    assert "v2:classes:new" in _callbacks(markup)
    assert "v2:platform" in _callbacks(markup)


def test_group_dashboard_does_not_expose_private_reminder_controls():
    markup = class_dashboard(12, can_review=True, can_manage=True, group_mode=True)
    labels = _labels(markup)
    callbacks = _callbacks(markup)

    assert "🗒 خلاصه امروز" in labels
    assert "🕓 صف بررسی" in labels
    assert not any("reminder" in callback for callback in callbacks)
    assert not any("manage" in callback for callback in callbacks)
    assert "v2:home" not in callbacks


def test_student_dashboard_does_not_expose_review_or_management():
    markup = class_dashboard(12, can_review=False, can_manage=False)
    labels = _labels(markup)

    assert "🕓 صف بررسی" not in labels
    assert "⚙️ مدیریت کلاس" not in labels
    assert "⬅️ بازگشت" in labels


def test_record_type_callbacks_are_class_scoped():
    callbacks = _callbacks(record_types(42))

    assert "v2:record:homework:42" in callbacks
    assert "v2:record:exam:42" in callbacks
    assert "v2:record:report:42" in callbacks
    assert "v2:class:open:42" in callbacks


def test_reminder_toggle_callback_contains_desired_next_state():
    markup = reminder_preferences(42, homework=True, exams=False)
    callbacks = _callbacks(markup)

    assert "v2:reminder:homework:42:0" in callbacks
    assert "v2:reminder:exams:42:1" in callbacks
