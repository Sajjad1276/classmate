from aiogram.fsm.state import State, StatesGroup


class RecordWizard(StatesGroup):
    choose_class = State()
    choose_subject = State()
    homework_title = State()
    homework_description = State()
    homework_due = State()
    exam_title = State()
    exam_scope = State()
    exam_datetime = State()
    report_date = State()
    report_body = State()
