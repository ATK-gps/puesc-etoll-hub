import requests
import json
import time
import os
import urllib.parse  # Добавлено для URL-кодирования

# --- КОНФИГУРАЦИЯ ---
# ВАЖНО: Убедитесь, что токен актуален
FLESPI_TOKEN = "BNwhrmoKyFAqUVKfGbcltAcX3kXAq5fmnxY5SzqduN4Pa0R4BUSVaoKsJOKu8IOJ"

# Имя файла с IMEI номерами (для формирования селекторов)
IMEI_FILENAME = "imeis.txt"
# Имя файла для записи ошибок
ERROR_LOG_FILENAME = "errors.log"
# Задержка между отправкой пакетов (в секундах)
DELAY_BETWEEN_BATCHES = 0.3
# Количество устройств, удаляемых в одном запросе (пакете)
BATCH_SIZE = 50
# URL API Flespi для удаления устройств
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


def delete_device_batch(imei_batch):
    """
    Формирует и отправляет запрос на УДАЛЕНИЕ пакета устройств в Flespi.
    """
    headers = {
        "Authorization": f"FlespiToken {FLESPI_TOKEN}",
        # "Content-Type": "application/json", # Больше не нужен, т.к. нет тела
        "Accept": "application/json"
    }

    # Flespi API позволяет удалять устройства по селекторам.
    #
    # ПОПРАВКА 3: Используем селектор-выражение (на основе присланной документации)
    # Формат: {configuration.ident=="IMEI1" || configuration.ident=="IMEI2"}
    # Это один селектор, который Flespi обрабатывает как "ИЛИ".
    # (Обратите внимание на == для сравнения строк внутри выражения)
    expression = " || ".join([f'configuration.ident=="{imei}"' for imei in imei_batch])
    selector_expression = f"{{{expression}}}"  # Оборачиваем в фигурные скобки

    # Важно: URL-кодируем селектор, т.к. он содержит спецсимволы {, }, |, "
    # (Как в примере curl: %%7B, %%22, %%7C%%7C)
    encoded_selector = urllib.parse.quote(selector_expression)

    # Собираем полный URL
    full_api_url = f"{API_URL}/{encoded_selector}"

    try:
        # Используем метод DELETE с новым URL и БЕЗ data
        response = requests.delete(full_api_url, headers=headers, timeout=30)
        # Вызовет исключение для кодов ответа 4xx/5xx
        response.raise_for_status()

        response_data = response.json()

        # При удалении Flespi возвращает "result": [null, null, ...] по одному null на каждое успешное удаление
        success_count = len(response_data.get('result', []))
        errors = response_data.get('errors', [])

        if errors:
            for error in errors:
                error_reason = error.get('reason', 'Неизвестная ошибка API')
                # Если устройство не найдено, Flespi тоже вернет ошибку, это нормально
                log_error(f"Ошибка от Flespi: {error_reason}")

        return success_count, len(errors)

    except requests.exceptions.HTTPError as http_err:
        error_details = f"Критическая HTTP ошибка: {http_err}"
        try:
            error_details += f" - Ответ сервера: {http_err.response.text}"
        except Exception:
            pass
        log_error(error_details)
        return 0, len(imei_batch)
    except requests.exceptions.RequestException as req_err:
        log_error(f"Ошибка сети или подключения: {req_err}")
        return 0, len(imei_batch)
    except json.JSONDecodeError:
        log_error("Ошибка: Не удалось декодировать JSON ответ от сервера Flespi.")
        return 0, len(imei_batch)


def main():
    """Основная функция для запуска скрипта."""
    print("--- Запуск скрипта для МАССОВОГО УДАЛЕНИЯ устройств в Flespi ---")

    imeis_to_delete = read_imeis_from_file(IMEI_FILENAME)
    if not imeis_to_delete:
        print("Список IMEI пуст или файл не найден. Завершение работы.")
        return

    print(f"Найдено уникальных IMEI для удаления: {len(imeis_to_delete)}")
    # Очистка старого лог-файла перед запуском
    if os.path.exists(ERROR_LOG_FILENAME):
        os.remove(ERROR_LOG_FILENAME)
        print(f"Старый лог-файл '{ERROR_LOG_FILENAME}' был удален.")

    total_success = 0
    total_errors = 0

    for i in range(0, len(imeis_to_delete), BATCH_SIZE):
        batch = imeis_to_delete[i:i + BATCH_SIZE]
        batch_number = (i // BATCH_SIZE) + 1
        total_batches = -(-len(imeis_to_delete) // BATCH_SIZE)  # Округление вверх

        print(f"\n-> Обработка пакета {batch_number} из {total_batches} (размер: {len(batch)} устройств)...")

        success, errors = delete_device_batch(batch)

        total_success += success
        total_errors += errors

        print(f"   Результат: Успешно удалено - {success}, Ошибок - {errors}")

        if i + BATCH_SIZE < len(imeis_to_delete):
            print(f"   Пауза {DELAY_BETWEEN_BATCHES} сек. перед следующим пакетом...")
            time.sleep(DELAY_BETWEEN_BATCHES)

    print("\n--- Работа скрипта завершена ---")
    print(f"Всего успешно удалено устройств: {total_success}")
    print(f"Всего не удалось удалить (ошибки): {total_errors}")
    if total_errors > 0:
        print(f"Подробности об ошибках записаны в файл: '{ERROR_LOG_FILENAME}'")


if __name__ == "__main__":
    main()