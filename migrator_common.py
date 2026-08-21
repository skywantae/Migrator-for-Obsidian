"""여러 마이그레이터가 함께 쓰는 것들."""


class Stopped(Exception):
    """사용자가 중단을 요청했을 때 발생."""
