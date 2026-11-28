"""Проверка живого Яндекс Календаря. Запускать из корня репозитория:

    python check_calendar.py --list          # показать названия календарей
    python check_calendar.py "Название"      # создать контрольное событие и прочитать обратно

Переменные окружения: YANDEX_CALDAV_EMAIL (полный логин) и YANDEX_CALDAV_KEY (пароль приложения).
"""

import os
import sys
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

URL = "https://caldav.yandex.ru"


def creds() -> tuple[str, str]:
    email, key = os.environ.get("YANDEX_CALDAV_EMAIL"), os.environ.get("YANDEX_CALDAV_KEY")
    if not email or not key:
        sys.exit("Задайте YANDEX_CALDAV_EMAIL и YANDEX_CALDAV_KEY (см. инструкцию).")
    return email, key


def list_calendars() -> None:
    import caldav

    email, key = creds()
    principal = caldav.DAVClient(url=URL, username=email, password=key, timeout=15).principal()
    for calendar in principal.calendars():
        print("Календарь:", calendar.name)


def check(name: str) -> bool:
    from app.calendar.caldav_adapter import CalDavCalendar
    from app.calendar.ports import EventSpec

    email, key = creds()
    calendar = CalDavCalendar(url=URL, username=email, password=key, calendars={"check": name}, timeout=15)
    tomorrow = (datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=1)).date()
    start = datetime.combine(tomorrow, time(12, 0), tzinfo=ZoneInfo("Europe/Moscow")).astimezone(UTC)
    spec = EventSpec(uid=f"check-{tomorrow:%Y%m%d}@iikuznezov", summary="ИИкузница: контрольное событие",
                     starts_at=start, ends_at=start + timedelta(minutes=30))
    window = (start - timedelta(hours=1), start + timedelta(hours=2))

    print("1. Чтение календаря...")
    before = calendar.list_events("check", *window)
    print(f"   событий в окне: {len(before)}")
    print("2. Запись контрольного события...")
    print("   id:", calendar.upsert_event("check", spec))
    print("3. Чтение обратно...")
    found = [e for e in calendar.list_events("check", *window) if e.uid == spec.uid]
    print("   найдено:", len(found), "(ожидаем 1)")
    print("4. Повторная запись (не должна создать дубль)...")
    calendar.upsert_event("check", spec)
    again = [e for e in calendar.list_events("check", *window) if e.uid == spec.uid]
    print("   найдено:", len(again), "(ожидаем 1)")
    ok = len(found) == 1 and len(again) == 1
    print("\nИТОГ:", "всё работает" if ok else "что-то не так, пришлите вывод разработчику")
    print(f"Откройте Яндекс Календарь: завтра ({tomorrow:%d.%m}) в 12:00 по Москве должно быть событие.")
    print("Потом удалите его вручную.")
    return ok


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    if sys.argv[1] == "--list":
        list_calendars()
    else:
        sys.exit(0 if check(sys.argv[1]) else 1)
