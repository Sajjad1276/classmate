from aiogram.fsm.state import State, StatesGroup


class V2Flow(StatesGroup):
    input = State()
