# -*- coding: utf-8 -*-
import os
import ssl
import time
import uuid
import base64
import hashlib
from pathlib import Path
from datetime import datetime, timezone
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

# ==============================================================================
# --- НАСТРОЙКИ (CONFIG) ---
# ==============================================================================

# Адрес боевого веб-сервиса PUESC (DocumentHandlingPort)
PUESC_URL = "https://ws.puesc.gov.pl/seap_wsChannel/DocumentHandlingPort"

# Авторизация PUESC
PUESC_LOGIN = "zam@atk-gps.by"
PUESC_PASSWORD = "HFxqCHa6eC-+47x"

# Данные оператора
OBE_SERVICE_NUMBER = "ZSL-GRNC-0"
OBE_OPERATOR_IDENTITY_TYPE = "INNY"
OBE_OPERATOR_IDENTITY_NUMBER = "BY291080284"

# Данные для блока ResponseWebService
RESPONSE_URL = "https://device-etoll.atkgps.pl/DocumentHandlingSvc"
RESPONSE_USERNAME = "zam@atk-gps.by"
RESPONSE_PASSWORD = "HFxqCHa6eC-+47x"
RESPONSE_CERT_FINGERPRINT = "91f371717516763d8fd3d60091877e23059d90ce"

# Префикс названия проекта
PROJECT_TAG = "delete"

# Настройки файлов
BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = BASE_DIR / "imei_list.txt"
OUTPUT_FOLDER = BASE_DIR / "output_xml"

CHUNK_SIZE = 20  # Количество устройств в одном пакете

# ==============================================================================


def get_ssl_context():
    """
    Формирует SSL-контекст для запросов.
    Предотвращает ошибку CERTIFICATE_VERIFY_FAILED на macOS при отсутствии certifi.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def get_daily_run_index(target_dir: Path, tag: str) -> int:
    """
    Определяет следующий порядковый номер запуска для текущего дня (dd.mm.yyyy).
    Ищет существующие файлы с шаблоном DD.MM.YYYY_tag_* и возвращает max(index) + 1.
    """
    today_str = datetime.now().strftime("%d.%m.%Y")
    max_idx = 0
    pattern = f"{today_str}_{tag}_"

    if target_dir.exists():
        for item in target_dir.iterdir():
            if item.is_file() and item.name.startswith(pattern):
                remainder = item.name[len(pattern):]
                part = remainder.split(".")[0].split("_")[0]
                if part.isdigit():
                    max_idx = max(max_idx, int(part))

    return max_idx + 1


def create_ws_security_credentials(password: str):
    """
    Формирует параметры аутентификации WS-Security UsernameToken (PasswordDigest)
    по официальной спецификации PUESC:
    Password_Digest = Base64(SHA-1(nonce + created + Base64(SHA-1(password))))
    """
    password_bytes = password.encode("utf-8")
    password_sha1 = hashlib.sha1(password_bytes).digest()
    password_hash = base64.b64encode(password_sha1).decode("utf-8")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    message_id = str(uuid.uuid4())
    nonce_bytes = os.urandom(16)
    nonce = base64.b64encode(nonce_bytes).decode("utf-8")

    data_to_digest = nonce_bytes + timestamp.encode("utf-8") + password_hash.encode("utf-8")
    digest_bytes = hashlib.sha1(data_to_digest).digest()
    digest_password = base64.b64encode(digest_bytes).decode("utf-8")

    return {
        "message_id": message_id,
        "timestamp": timestamp,
        "nonce": nonce,
        "digest_password": digest_password,
    }


def read_imei_list(filepath: Path):
    """Читает список IMEI из файла."""
    if not filepath.exists():
        print(f"❌ ОШИБКА: Входной файл '{filepath}' не найден!")
        return []

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            imeis = [line.strip().split(";")[0].strip() for line in f if line.strip()]
        unique_imeis = list(dict.fromkeys(imeis))
        return unique_imeis
    except Exception as e:
        print(f"❌ Ошибка чтения файла '{filepath}': {e}")
        return []


def create_delete_xml_chunk(imei_chunk):
    """Формирует XML документ ZSL_120 для удаления списка IMEI."""
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<ns1:ZSL_120 xmlns:ns1="http://www.mf.gov.pl/SENT/2020/07/21/ZSL_120.xsd" xmlns:ns2="http://www.mf.gov.pl/SENT/2020/07/21/ZTypes.xsd">
    <ns1:OBEServiceNumber>{OBE_SERVICE_NUMBER}</ns1:OBEServiceNumber>
    <ns1:OBEOperatorIdentityType>{OBE_OPERATOR_IDENTITY_TYPE}</ns1:OBEOperatorIdentityType>
    <ns1:OBEOperatorIdentityNumber>{OBE_OPERATOR_IDENTITY_NUMBER}</ns1:OBEOperatorIdentityNumber>
"""
    for imei in imei_chunk:
        xml_content += f"    <ns1:GPSDeviceToRemove>{imei}</ns1:GPSDeviceToRemove>\n"

    xml_content += f"""    <ns1:ResponseWebService>
        <ns2:UrlAddress>{RESPONSE_URL}</ns2:UrlAddress>
        <ns2:UserName>{RESPONSE_USERNAME}</ns2:UserName>
        <ns2:UserPassword>{RESPONSE_PASSWORD}</ns2:UserPassword>
        <ns2:CertificateFingerPrint>{RESPONSE_CERT_FINGERPRINT}</ns2:CertificateFingerPrint>
    </ns1:ResponseWebService>
</ns1:ZSL_120>
"""
    return xml_content


def create_zsl122_status_query(imei: str):
    """Формирует XML ZSL_122 для проверки статуса конкретного устройства."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ns1:ZSL_122 xmlns:ns1="http://www.mf.gov.pl/SENT/2020/07/21/ZSL_122.xsd" xmlns:ns2="http://www.mf.gov.pl/SENT/2020/07/21/ZTypes.xsd">
    <ns1:OBEOperatorIdentityType>{OBE_OPERATOR_IDENTITY_TYPE}</ns1:OBEOperatorIdentityType>
    <ns1:OBEOperatorIdentityNumber>{OBE_OPERATOR_IDENTITY_NUMBER}</ns1:OBEOperatorIdentityNumber>
    <ns1:OBEDevicesSearchAttributes>
        <ns2:OBEServiceNumber>{OBE_SERVICE_NUMBER}</ns2:OBEServiceNumber>
        <ns2:GPSDeviceID>{imei}</ns2:GPSDeviceID>
    </ns1:OBEDevicesSearchAttributes>
    <ns1:OBEDevicesLimitValues>
        <ns2:LimitFrom>1</ns2:LimitFrom>
        <ns2:LimitTo>10</ns2:LimitTo>
    </ns1:OBEDevicesLimitValues>
    <ns1:OBEDevicesSortAttributes>
        <ns2:SortAttributeName>GPSDeviceID</ns2:SortAttributeName>
        <ns2:SortOrder>ASC</ns2:SortOrder>
    </ns1:OBEDevicesSortAttributes>
    <ns1:ResponseWebService>
        <ns2:UrlAddress>{RESPONSE_URL}</ns2:UrlAddress>
        <ns2:UserName>{RESPONSE_USERNAME}</ns2:UserName>
        <ns2:UserPassword>{RESPONSE_PASSWORD}</ns2:UserPassword>
        <ns2:CertificateFingerPrint>{RESPONSE_CERT_FINGERPRINT}</ns2:CertificateFingerPrint>
    </ns1:ResponseWebService>
</ns1:ZSL_122>"""


def send_document_to_puesc(xml_content: str, filename: str = "ZSL_120_delete.xml"):
    """
    Оборачивает XML документ в SOAP-пакет AcceptDocumentRequest и отправляет на PUESC.
    Возвращает кортеж (успех: bool, sysRef: str|None, raw_body: str).
    """
    try:
        creds = create_ws_security_credentials(PUESC_PASSWORD)
        content_b64 = base64.b64encode(xml_content.encode("utf-8")).decode("utf-8")

        soap_envelope = f"""<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
    <soap-env:Header xmlns:wsa="http://www.w3.org/2005/08/addressing">
        <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <wsse:UsernameToken>
                <wsse:Username>{PUESC_LOGIN}</wsse:Username>
                <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{creds["digest_password"]}</wsse:Password>
                <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{creds["nonce"]}</wsse:Nonce>
                <wsu:Created xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{creds["timestamp"]}</wsu:Created>
            </wsse:UsernameToken>
        </wsse:Security>
        <wsa:Action>AcceptDocument</wsa:Action>
        <wsa:MessageID>uuid:{creds["message_id"]}</wsa:MessageID>
        <wsa:To>{PUESC_URL}</wsa:To>
    </soap-env:Header>
    <soap-env:Body>
        <_v2:AcceptDocumentRequest xmlns:_v2="http://www.mf.gov.pl/uslugiBiznesowe/WsPull/Usluga/2014/01_v2_0">
            <_v21:document xmlns:_v21="http://www.mf.gov.pl/schematy/SISC/WsChannel/2014/01_v2_0">
                <_v21:content filename='{filename}' mime="application/xml">{content_b64}</_v21:content>
                <_v21:targetSystems>
                    <_v21:system>SENT</_v21:system>
                </_v21:targetSystems>
            </_v21:document>
        </_v2:AcceptDocumentRequest>
    </soap-env:Body>
</soap-env:Envelope>"""

        headers = {
            "Content-Type": "application/xml; charset=utf-8",
            "SOAPAction": "AcceptDocument",
        }

        req = urllib.request.Request(PUESC_URL, data=soap_envelope.encode("utf-8"), headers=headers, method="POST")
        ssl_ctx = get_ssl_context()

        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
            resp_body = resp.read().decode("utf-8")
            root = ET.fromstring(resp_body)
            sys_ref_elem = root.find(".//{*}sysRef")
            sys_ref = sys_ref_elem.text if sys_ref_elem is not None else None
            return True, sys_ref, resp_body

    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        return False, None, err_body
    except Exception as ex:
        return False, None, str(ex)


def poll_puesc_document():
    """Вычитывает один документ из входящей очереди PUESC (GetNextDocument)."""
    try:
        creds = create_ws_security_credentials(PUESC_PASSWORD)
        soap_envelope = f"""<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
    <soap-env:Header xmlns:wsa="http://www.w3.org/2005/08/addressing">
        <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <wsse:UsernameToken>
                <wsse:Username>{PUESC_LOGIN}</wsse:Username>
                <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{creds["digest_password"]}</wsse:Password>
                <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{creds["nonce"]}</wsse:Nonce>
                <wsu:Created xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{creds["timestamp"]}</wsu:Created>
            </wsse:UsernameToken>
        </wsse:Security>
        <wsa:Action>GetNextDocument</wsa:Action>
        <wsa:MessageID>uuid:{creds["message_id"]}</wsa:MessageID>
        <wsa:To>{PUESC_URL}</wsa:To>
    </soap-env:Header>
    <soap-env:Body>
        <_v2:GetNextDocumentRequest xmlns:_v2="http://www.mf.gov.pl/uslugiBiznesowe/WsPull/Usluga/2014/01_v2_0">
            <_v2:targetSystem>SENT</_v2:targetSystem>
        </_v2:GetNextDocumentRequest>
    </soap-env:Body>
</soap-env:Envelope>"""

        headers = {
            "Content-Type": "application/xml; charset=utf-8",
            "SOAPAction": "GetNextDocument",
        }

        req = urllib.request.Request(PUESC_URL, data=soap_envelope.encode("utf-8"), headers=headers, method="POST")
        ssl_ctx = get_ssl_context()

        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
            resp_body = resp.read().decode("utf-8")
            root = ET.fromstring(resp_body)
            content_elem = root.find(".//{*}content")

            if content_elem is None or not content_elem.text:
                return None, None

            filename = content_elem.get("filename", "unknown.xml")
            decoded_xml = base64.b64decode(content_elem.text).decode("utf-8")
            return filename, decoded_xml
    except Exception:
        return None, None


def check_existing_device_removal_status(missing_imeis: set, max_wait_sec: int = 20):
    """
    Для IMEI, которые не вернулись в ZSL_121 (например, уже были удалены ранее или не найдены),
    запрашивает их актуальный статус через ZSL_122.
    """
    already_removed = {}
    not_found = []

    for imei in missing_imeis:
        try:
            zsl122_xml = create_zsl122_status_query(imei)
            success, sys_ref, _ = send_document_to_puesc(zsl122_xml, filename="ZSL_122_check.xml")

            if not success:
                not_found.append({"deviceId": imei, "reason": "Не удалось отправить запрос статуса"})
                continue

            start_t = time.time()
            found = False
            while time.time() - start_t < max_wait_sec:
                time.sleep(2)
                fn, decoded_xml = poll_puesc_document()
                if not decoded_xml:
                    continue

                try:
                    doc_root = ET.fromstring(decoded_xml)
                    for dev in doc_root.findall(".//{*}OBEDevicesData"):
                        dev_id = (dev.findtext("{*}GPSDeviceID") or "").strip()
                        if dev_id == imei:
                            status = dev.findtext("{*}GPSDeviceStatus", default="")
                            loc_num = dev.findtext("{*}GeoLocatorNumber", default="")
                            mod_date = dev.findtext("{*}ModificationDate") or dev.findtext("{*}CreationDate") or ""

                            if status in ("4", "1"):
                                already_removed[dev_id] = {
                                    "deviceId": dev_id,
                                    "locNum": loc_num,
                                    "status": status,
                                    "modDate": mod_date,
                                    "already_removed": True,
                                }
                            else:
                                already_removed[dev_id] = {
                                    "deviceId": dev_id,
                                    "locNum": loc_num,
                                    "status": status,
                                    "modDate": mod_date,
                                    "already_removed": False,
                                }
                            found = True
                            break
                    if found:
                        break
                except Exception:
                    pass

            if not found:
                not_found.append({"deviceId": imei, "reason": "Устройство не найдено в реестре PUESC (никогда не регистрировалось или удалено)"})
        except Exception as e:
            not_found.append({"deviceId": imei, "reason": f"Ошибка проверки: {e}"})

    return already_removed, not_found


def wait_and_fetch_removal_response(target_imeis: set, response_xml_path: Path, max_wait_sec: int = 20, poll_interval: int = 3):
    """
    Ожидает подтверждения удаления устройств (ZSL_121 -> DevicesRemoved / DevicesFailed).
    Если часть устройств уже была удалена ранее, автоматически проверяет их статус.
    """
    start_time = time.time()
    removed_now = {}
    already_removed = {}
    failed_devices = []

    while time.time() - start_time < max_wait_sec:
        while True:
            filename, decoded_xml = poll_puesc_document()
            if not decoded_xml:
                break

            try:
                doc_root = ET.fromstring(decoded_xml)

                # 1. Проверяем успешно удаленные в текущем запросе (DevicesRemoved)
                for d in doc_root.findall(".//{*}DevicesRemoved"):
                    dev_id = (d.findtext("{*}GPSDeviceID") or "").strip()
                    if dev_id in target_imeis:
                        removed_now[dev_id] = {
                            "deviceId": dev_id,
                            "locNum": d.findtext("{*}GeoLocatorNumber", default=""),
                            "status": d.findtext("{*}GPSDeviceStatus", default="4"),
                            "modDate": d.findtext("{*}ModificationDate", default=""),
                        }

                # 2. Проверяем ошибки удаления (DevicesFailed)
                for d in doc_root.findall(".//{*}DevicesFailed"):
                    dev_id = (d.findtext("{*}GPSDeviceID") or "").strip()
                    if dev_id in target_imeis:
                        reason = d.findtext("{*}Reason", default="Ошибка удаления / устройство уже деактивировано")
                        failed_devices.append({"deviceId": dev_id, "reason": reason})

                if len(removed_now) + len(failed_devices) >= len(target_imeis):
                    try:
                        with open(response_xml_path, "w", encoding="utf-8") as rf:
                            rf.write(decoded_xml)
                    except Exception:
                        pass
                    return removed_now, already_removed, failed_devices

            except Exception:
                pass

        if len(removed_now) + len(failed_devices) >= len(target_imeis):
            return removed_now, already_removed, failed_devices

        elapsed = int(time.time() - start_time)
        print(f"   ⏳ Ожидание подтверждения удаления PUESC... ({elapsed}/{max_wait_sec} сек.)")
        time.sleep(poll_interval)

    # Для устройств, которые не вернулись в ZSL_121 (например, уже удалены ранее), проверяем статус
    missing = target_imeis - set(removed_now.keys()) - {f['deviceId'] for f in failed_devices}
    if missing:
        print(f"\nℹ Проверка статуса для устройств, не вернувшихся в ZSL_121: {', '.join(missing)}...")
        ar, nf = check_existing_device_removal_status(missing)
        already_removed.update(ar)
        failed_devices.extend(nf)

    return removed_now, already_removed, failed_devices


# FLESPI CONFIG
FLESPI_TOKEN = "BNwhrmoKyFAqUVKfGbcltAcX3kXAq5fmnxY5SzqduN4Pa0R4BUSVaoKsJOKu8IOJ"
FLESPI_BASE_URL = "https://flespi.io/gw"


def delete_devices_from_flespi(imeis_list: list):
    """
    Массово удаляет устройства из Flespi по списку IMEI через REST API.
    """
    clean_imeis = [str(i).strip() for i in imeis_list if str(i).strip()]
    if not clean_imeis:
        return 0, 0

    import urllib.parse
    headers = {
        "Authorization": f"FlespiToken {FLESPI_TOKEN}",
        "Accept": "application/json"
    }

    # Flespi API селектор: {configuration.ident=="IMEI1" || configuration.ident=="IMEI2"}
    expression = " || ".join([f'configuration.ident=="{imei}"' for imei in clean_imeis])
    selector = f"{{{expression}}}"
    encoded_selector = urllib.parse.quote(selector)
    url = f"{FLESPI_BASE_URL}/devices/{encoded_selector}"

    req = urllib.request.Request(url, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            deleted_count = len(data.get("result", []))
            errors_count = len(data.get("errors", []))
            return deleted_count, errors_count
    except Exception as ex:
        print(f"⚠ Ошибка при удалении из Flespi: {ex}")
        return 0, len(clean_imeis)


def save_removal_results_report(removed_now: dict, already_removed: dict, failed_devices: list, flespi_deleted: int, txt_path: Path, csv_path: Path):
    """
    Формирует подробный отчет об удалении в TXT и CSV форматы (PUESC + Flespi).
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Сохраняем подробный CSV
    try:
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("IMEI;GeoLocatorNumber;Status;ModificationDate;Result;FlespiStatus\n")
            for imei, d in removed_now.items():
                f.write(f"{d['deviceId']};{d['locNum']};{d['status']};{d['modDate']};REMOVED_NOW;REMOVED_FROM_FLESPI\n")
            for imei, d in already_removed.items():
                f.write(f"{d['deviceId']};{d['locNum']};{d['status']};{d['modDate']};ALREADY_REMOVED;REMOVED_FROM_FLESPI\n")
            for f_item in failed_devices:
                f.write(f"{f_item['deviceId']};;;;{f_item['reason']};REMOVED_FROM_FLESPI\n")
    except Exception as e:
        print(f"⚠ Ошибка сохранения CSV-отчета: {e}")

    # 2. Сохраняем красивый структурированный TXT-отчет
    try:
        total_count = len(removed_now) + len(already_removed) + len(failed_devices)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("=" * 85 + "\n")
            f.write("ОТЧЕТ: РЕЗУЛЬТАТЫ УДАЛЕНИЯ УСТРОЙСТВ ИЗ PUESC (e-TOLL) & FLESPI\n")
            f.write(f"Оператор: {OBE_SERVICE_NUMBER} ({OBE_OPERATOR_IDENTITY_NUMBER})\n")
            f.write(f"Дата формирования отчета: {now_str}\n")
            f.write(f"Всего обработано устройств: {total_count}\n")
            f.write(f"Удалено объектов из Flespi:  {flespi_deleted}\n")
            f.write("=" * 85 + "\n\n")

            if removed_now:
                f.write("--- 1. УСПЕШНО УДАЛЕНЫ В ДАННОЙ СЕССИИ ---\n")
                f.write(f"{'IMEI':<18} | {'Номер локатора (ID)':<22} | {'Статус':<8} | {'Дата удаления'}\n")
                f.write("-" * 85 + "\n")
                for imei, d in removed_now.items():
                    f.write(f"{d['deviceId']:<18} | {d['locNum']:<22} | {d['status']:<8} | {d['modDate']}\n")
                f.write("\n")

            if already_removed:
                f.write("--- 2. УЖЕ БЫЛИ УДАЛЕНЫ РАНЕЕ В PUESC ---\n")
                f.write(f"{'IMEI':<18} | {'Номер локатора (ID)':<22} | {'Статус':<8} | {'Дата последней модификации'}\n")
                f.write("-" * 85 + "\n")
                for imei, d in already_removed.items():
                    f.write(f"{d['deviceId']:<18} | {d['locNum']:<22} | {d['status']:<8} | {d['modDate']}\n")
                f.write("\n")

            if failed_devices:
                f.write("--- 3. НЕ НАЙДЕНЫ / ОШИБКИ ---\n")
                f.write(f"{'IMEI':<18} | {'Причина / Статус'}\n")
                f.write("-" * 85 + "\n")
                for f_item in failed_devices:
                    f.write(f"{f_item['deviceId']:<18} | {f_item['reason']}\n")
                f.write("\n")

            f.write("=" * 85 + "\n")
    except Exception as e:
        print(f"⚠ Ошибка сохранения TXT-отчета: {e}")

    print(f"\n📄 Итоговые документы с результатами удаления сформированы:")
    print(f"   ├─ Текстовый отчет: {txt_path.name}")
    print(f"   └─ CSV-таблица:     {csv_path.name}")


def main():
    """Основной процесс удаления устройств из PUESC."""
    print("=" * 70)
    print("🗑 Удаление устройств из PUESC (e-TOLL / SENT)")
    print("=" * 70)

    # 1. Создаем папку для сохранения XML
    try:
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # 2. Читаем список IMEI
    all_imeis = read_imei_list(INPUT_FILE)
    if not all_imeis:
        print(f"⚠ Входной файл '{INPUT_FILE}' пуст или не найден. Завершение работы.")
        return

    print(f"📄 Загружено уникальных IMEI: {len(all_imeis)}")

    # 3. Определяем имя файлов запуска по шаблону dd.mm.yyyy_delete_N
    today_str = datetime.now().strftime("%d.%m.%Y")
    run_idx = get_daily_run_index(BASE_DIR, PROJECT_TAG)
    file_prefix = f"{today_str}_{PROJECT_TAG}_{run_idx}"

    result_txt_path = BASE_DIR / f"{file_prefix}.txt"
    result_csv_path = BASE_DIR / f"{file_prefix}.csv"
    response_xml_path = BASE_DIR / f"{file_prefix}_response.xml"

    print(f"📝 Порядковый номер запуска сегодня ({today_str}): #{run_idx}")
    print(f"📁 Базовое имя файлов: {file_prefix}.*")

    # 4. Разбиваем на части по CHUNK_SIZE
    chunks = [all_imeis[i:i + CHUNK_SIZE] for i in range(0, len(all_imeis), CHUNK_SIZE)]
    print(f"📦 Сформировано пакетов для отправки: {len(chunks)} (по {CHUNK_SIZE} шт.)\n")

    all_removed_now = {}
    all_already_removed = {}
    all_failed = []

    for i, chunk in enumerate(chunks, 1):
        filename = f"{file_prefix}_part_{i}.xml"
        filepath = OUTPUT_FOLDER / filename

        print(f"--- Пакет [{i}/{len(chunks)}]: {len(chunk)} устройств ---")

        # Формируем и сохраняем XML локально
        try:
            xml_content = create_delete_xml_chunk(chunk)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(xml_content)
            print(f"💾 Локальный XML сохранен: {filepath.name}")
        except Exception as ex:
            print(f"⚠ Не удалось сохранить локальный XML: {ex}")
            xml_content = create_delete_xml_chunk(chunk)

        # Отправляем в PUESC
        print(f"📡 Отправка пакета на удаление в PUESC...")
        success, sys_ref, err_msg = send_document_to_puesc(xml_content, filename=filename)

        if not success:
            print(f"❌ Ошибка отправки пакета {i} в PUESC: {err_msg}")
            for imei in chunk:
                all_failed.append({"deviceId": imei, "reason": f"Ошибка отправки: {err_msg}"})
            continue

        print(f"✅ Пакет принят PUESC (sysRef: {sys_ref})")

        # Ожидаем подтверждение удаления
        target_chunk_set = set(chunk)
        rem_now, alr_rem, failed = wait_and_fetch_removal_response(target_chunk_set, response_xml_path, max_wait_sec=20, poll_interval=3)

        all_removed_now.update(rem_now)
        all_already_removed.update(alr_rem)
        all_failed.extend(failed)

        if rem_now:
            print(f"  ✅ Подтверждено удаление: {len(rem_now)} устройств")
        if alr_rem:
            print(f"  ℹ Уже были удалены ранее: {len(alr_rem)} устройств")
        if failed:
            print(f"  ⚠ Требуют внимания / не найдены: {len(failed)} устройств")

        time.sleep(1)

    print("\n" + "=" * 70)
    print("📊 ИТОГОВЫЙ ОТЧЕТ ОБ УДАЛЕНИИ В PUESC")
    print("=" * 70)

    if all_removed_now:
        print(f"\n✅ Успешно удалено в этой сессии: {len(all_removed_now)}")
        for imei, d in all_removed_now.items():
            print(f"  • IMEI: {d['deviceId']} | Номер: {d['locNum']} | Дата: {d['modDate']}")

    if all_already_removed:
        print(f"\nℹ Уже были удалены ранее в PUESC: {len(all_already_removed)}")
        for imei, d in all_already_removed.items():
            print(f"  • IMEI: {d['deviceId']} | Номер: {d['locNum']} | Статус: {d['status']} (ДЕАКТИВИРОВАНО)")

    if all_failed:
        print(f"\n❌ Не найдены в реестре / ошибки: {len(all_failed)}")
        for f_item in all_failed:
            print(f"  • IMEI: {f_item['deviceId']} — {f_item['reason']}")

    # 5. Удаляем устройства из Flespi
    print("\n" + "=" * 70)
    print("🗑 Удаление устройств из платформы Flespi...")
    print("=" * 70)
    flespi_deleted, flespi_errors = delete_devices_from_flespi(all_imeis)
    print(f"✅ Удалено устройств из Flespi: {flespi_deleted}")
    if flespi_errors:
        print(f"ℹ Не требовали удаления / не были созданы в Flespi: {flespi_errors}")

    # Сохраняем документы с результатами
    save_removal_results_report(all_removed_now, all_already_removed, all_failed, flespi_deleted, result_txt_path, result_csv_path)

    print("\n🏁 Работа скрипта удаления (PUESC + Flespi) завершена.")


if __name__ == "__main__":
    main()