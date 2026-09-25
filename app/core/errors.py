class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def not_found(name: str) -> AppError:
    return AppError(f"{name.upper()}_NOT_FOUND", f"{name.replace('_', ' ').title()} not found", 404)


def conflict(code: str, message: str) -> AppError:
    return AppError(code, message, 409)
