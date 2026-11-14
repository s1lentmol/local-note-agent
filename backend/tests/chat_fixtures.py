from app.model_clients.contracts import Generation, ToolCall


def tools(*calls):
    return Generation(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[
            ToolCall(id=f"call_{index}", name=name, arguments=arguments)
            for index, (name, arguments) in enumerate(calls)
        ],
    )


def answer(text="Данные из заметки"):
    return Generation(content=text, finish_reason="stop")
