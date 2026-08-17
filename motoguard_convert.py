import json
from datetime import datetime

# --- НАСТРОЙКИ ---
# Укажите имя вашего исходного файла (он должен лежать рядом со скриптом)
input_file_path = 'response-7.json'
# Имя файла, в который сохранится результат
output_file_path = 'processed_data.json'

try:
    # 1. Открываем и загружаем исходный файл
    with open(input_file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    processed_list = []

    # 2. Обрабатываем список
    # Проверяем, есть ли ключ 'result', если нет - пробуем читать как простой список
    source_list = data.get('result', data) if isinstance(data, dict) else data

    for item in source_list:
        # Получаем данные, используем .get для безопасности (если ключа нет)
        name = item.get('name', 'Unknown')
        timestamp = item.get('last_active', 0)

        if timestamp == 0:
            readable_time = "Nigdy"
        else:
            # Конвертация timestamp в читаемую дату (UTC)
            readable_time = datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')

        processed_list.append({
            "name": name,
            "last_active": readable_time
        })

    # 3. Сохраняем результат в новый файл
    # Создаем структуру как в оригинале (с ключом result)
    output_data = {"result": processed_list}

    with open(output_file_path, 'w', encoding='utf-8') as f:
        # ensure_ascii=False позволяет сохранять русские буквы ("Никогда") читаемыми
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    print(f"✅ Успешно! Обработано {len(processed_list)} записей.")
    print(f"Результат сохранен в файл: {output_file_path}")

except FileNotFoundError:
    print(f"❌ Ошибка: Файл '{input_file_path}' не найден в папке со скриптом.")
except json.JSONDecodeError:
    print(f"❌ Ошибка: Не удалось прочитать JSON. Проверьте структуру файла.")
except Exception as e:
    print(f"❌ Произошла непредвиденная ошибка: {e}")