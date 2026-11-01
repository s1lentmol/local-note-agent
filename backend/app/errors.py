from fastapi import HTTPException


class ProcessingError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

    def as_dict(self):
        return {"code": self.code, "message": self.message}


def api_error(status: int, code: str, message: str):
    return HTTPException(status, {"code": code, "message": message})
