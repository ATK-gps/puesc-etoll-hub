import requests
import json
import time
import os
from pathlib import Path

# --- КОНФИГУРАЦИЯ ---
# ВАЖНО: Замените на ваш актуальный токен Flespi
FLESPI_TOKEN = "BNwhrmoKyFAqUVKfGbcltAcX3kXAq5fmnxY5SzqduN4Pa0R4BUSVaoKsJOKu8IOJ"

# ID протокола (14 = Teltonika, определено из логов)
PROTOCOL_ID = 15
# ID типа устройства (22, определено из логов)
# Если вы добавляете другие типы устройств, это значение может потребоваться изменить.
DEVICE_TYPE_ID = 14

# Имя файла с IMEI номерами
IMEI_FILENAME = Path("/Users/aleksandrslozenikin/PycharmProjects/Flespi-e-toll/overseer/imeis.txt")
# Имя файла для записи ошибок
ERROR_LOG_FILENAME = Path("/Users/aleksandrslozenikin/PycharmProjects/Flespi-e-toll/overseer/errors.log")
# Задержка между отправкой пакетов (в секундах)
DELAY_BETWEEN_BATCHES = 0.3
# Количество устройств, отправляемых в одном запросе (пакете)
BATCH_SIZE = 50
# URL API Flespi для создания устройств
API_URL = "https://flespi.io/gw/devices"


def read_imeis_from_file(filename):
    """
    Читает IMEI из файла.
    Удаляет дубликаты, пробелы и пустые строки.
    """
    if not os.path.exists(filename):
        print(f"Ошибка: Файл с IMEI '{filename}' не найден.")
        return []
    try:
        with open(filename, 'r') as f:
            # Используем set для автоматического удаления дубликатов
            imeis = {line.strip() for line in f if line.strip().isdigit()}
        return list(imeis)
    except Exception as e:
        print(f"Не удалось прочитать файл '{filename}': {e}")
        return []


def log_error(error_message):
    """Записывает сообщение об ошибке в лог-файл с временной меткой."""
    with open(ERROR_LOG_FILENAME, 'a', encoding='utf-8') as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {error_message}\n")


def create_device_batch(imei_batch):
    """
    Формирует и отправляет запрос на создание пакета устройств в Flespi.
    """
    headers = {
        "Authorization": f"FlespiToken {FLESPI_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    payload = []
    for imei in imei_batch:
        device_data = {
            "name": f"overseer {imei}",
            "device_type_id": DEVICE_TYPE_ID,
            "messages_ttl": 1209600,  # 14 дней в секундах (14 * 24 * 60 * 60)
            "configuration": {
                "ident": imei
            }
        }
        payload.append(device_data)

    try:
        response = requests.post(API_URL, headers=headers, data=json.dumps(payload), timeout=30)
        # Вызовет исключение для кодов ответа 4xx/5xx
        response.raise_for_status()

        response_data = response.json()
        success_count = len(response_data.get('result', []))
        errors = response_data.get('errors', [])

        if errors:
            for error in errors:
                # API Flespi возвращает детальную причину ошибки
                error_reason = error.get('reason', 'Неизвестная ошибка API')
                log_error(f"Ошибка от Flespi: {error_reason}")

        return success_count, len(errors)

    except requests.exceptions.HTTPError as http_err:
        error_details = f"Критическая HTTP ошибка: {http_err}"
        try:
            # Попытка получить более детальную информацию из тела ответа
            error_details += f" - Ответ сервера: {http_err.response.text}"
        except Exception:
            pass  # Если тело ответа пустое или нечитаемое
        log_error(error_details)
        # В случае ошибки всего пакета, считаем, что все IMEI в нем не удалось добавить
        return 0, len(imei_batch)
    except requests.exceptions.RequestException as req_err:
        log_error(f"Ошибка сети или подключения: {req_err}")
        return 0, len(imei_batch)
    except json.JSONDecodeError:
        log_error("Ошибка: Не удалось декодировать JSON ответ от сервера Flespi.")
        return 0, len(imei_batch)


def main():
    """Основная функция для запуска скрипта."""
    print("--- Запуск скрипта для автоматического добавления устройств в Flespi ---")

    imeis_to_add = read_imeis_from_file(IMEI_FILENAME)
    if not imeis_to_add:
        print("Список IMEI пуст или файл не найден. Завершение работы.")
        return

    print(f"Найдено уникальных IMEI для добавления: {len(imeis_to_add)}")
    # Очистка старого лог-файла перед запуском
    if os.path.exists(ERROR_LOG_FILENAME):
        os.remove(ERROR_LOG_FILENAME)
        print(f"Старый лог-файл '{ERROR_LOG_FILENAME}' был удален.")

    total_success = 0
    total_errors = 0

    # Разделение всего списка IMEI на пакеты (batches) для отправки
    for i in range(0, len(imeis_to_add), BATCH_SIZE):
        batch = imeis_to_add[i:i + BATCH_SIZE]
        batch_number = (i // BATCH_SIZE) + 1
        total_batches = -(-len(imeis_to_add) // BATCH_SIZE)  # Округление вверх

        print(f"\n-> Обработка пакета {batch_number} из {total_batches} (размер: {len(batch)} устройств)...")

        success, errors = create_device_batch(batch)

        total_success += success
        total_errors += errors

        print(f"   Результат: Успешно добавлено - {success}, Ошибок - {errors}")

        # Проверяем, является ли текущий пакет не последним
        if i + BATCH_SIZE < len(imeis_to_add):
            print(f"   Пауза {DELAY_BETWEEN_BATCHES} сек. перед следующим пакетом...")
            time.sleep(DELAY_BETWEEN_BATCHES)

    print("\n--- Работа скрипта завершена ---")
    print(f"Всего успешно добавлено устройств: {total_success}")
    print(f"Всего не удалось добавить (ошибки): {total_errors}")
    if total_errors > 0:
        print(f"Подробности об ошибках записаны в файл: '{ERROR_LOG_FILENAME}'")


if __name__ == "__main__":
    main()

