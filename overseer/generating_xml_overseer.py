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
PROJECT_TAG = "overseer"

# Имена файлов (относительно расположения скрипта)
BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = BASE_DIR / "imeis_for_puesc.txt"

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


def create_puesc_xml(lines):
    """
    Формирует XML документ ZSL_120 на основе строк из файла.
    Возвращает (xml_content: str, device_count: int, target_imeis: set).
    """
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<ns1:ZSL_120 xmlns:ns1="http://www.mf.gov.pl/SENT/2020/07/21/ZSL_120.xsd" xmlns:ns2="http://www.mf.gov.pl/SENT/2020/07/21/ZTypes.xsd">
    <ns1:OBEServiceNumber>{OBE_SERVICE_NUMBER}</ns1:OBEServiceNumber>
    <ns1:OBEOperatorIdentityType>{OBE_OPERATOR_IDENTITY_TYPE}</ns1:OBEOperatorIdentityType>
    <ns1:OBEOperatorIdentityNumber>{OBE_OPERATOR_IDENTITY_NUMBER}</ns1:OBEOperatorIdentityNumber>
"""
    device_count = 0
    target_imeis = set()

    for line in lines:
        parts = line.split(";")
        imei = parts[0].strip()
        add_info = parts[1].strip() if len(parts) > 1 and parts[1].strip() else f"overseer {imei}"

        if imei:
            target_imeis.add(imei)
            xml_content += f"""    <ns1:GPSDeviceToAdd>
        <ns2:GPSDeviceID>{imei}</ns2:GPSDeviceID>
        <ns2:AdditionalInformation>{add_info}</ns2:AdditionalInformation>
    </ns1:GPSDeviceToAdd>
"""
            device_count += 1

    xml_content += f"""    <ns1:ResponseWebService>
        <ns2:UrlAddress>{RESPONSE_URL}</ns2:UrlAddress>
        <ns2:UserName>{RESPONSE_USERNAME}</ns2:UserName>
        <ns2:UserPassword>{RESPONSE_PASSWORD}</ns2:UserPassword>
        <ns2:CertificateFingerPrint>{RESPONSE_CERT_FINGERPRINT}</ns2:CertificateFingerPrint>
    </ns1:ResponseWebService>
</ns1:ZSL_120>
"""
    return xml_content, device_count, target_imeis


def create_zsl122_query_xml(imei: str):
    """
    Формирует XML документ ZSL_122 для поиска/запроса данных уже зарегистрированного устройства.
    """
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


def send_document_to_puesc(xml_content: str, filename: str = "ZSL_120.xml"):
    """
    Оборачивает XML документ в SOAP-пакет AcceptDocumentRequest и отправляет на PUESC.
    Возвращает кортеж (успех: bool, sysRef: str|None, raw_body: str).
    """
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

    try:
        ssl_ctx = get_ssl_context()
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
            resp_body = resp.read().decode("utf-8")
            root = ET.fromstring(resp_body)
            sys_ref_elem = root.find(".//{*}sysRef")
            sys_ref = sys_ref_elem.text if sys_ref_elem is not None else None

            if sys_ref:
                return True, sys_ref, resp_body
            else:
                return True, None, resp_body

    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        return False, None, err_body
    except Exception as ex:
        return False, None, str(ex)


def poll_puesc_document():
    """
    Вычитывает один документ из входящей очереди PUESC (GetNextDocument).
    Возвращает (filename, decoded_xml) или (None, None) если очередь пуста.
    """
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

    try:
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


def query_existing_devices_info(missing_imeis: set, max_wait_sec: int = 30):
    """
    Для IMEI, которые уже были зарегистрированы ранее (не вернулись в ZSL_121),
    отправляет поисковый запрос ZSL_122 и получает актуальные реквизиты (ZSL_123).
    """
    found_existing = {}

    for imei in missing_imeis:
        print(f"🔍 Запрос данных существующего устройства в PUESC (ZSL_122) для IMEI: {imei}...")
        zsl122_xml = create_zsl122_query_xml(imei)
        success, sys_ref, _ = send_document_to_puesc(zsl122_xml, filename="ZSL_122.xml")

        if not success:
            continue

        # Ожидаем ZSL_123 в очереди
        start_t = time.time()
        while time.time() - start_t < max_wait_sec:
            time.sleep(3)
            fn, decoded_xml = poll_puesc_document()
            if not decoded_xml:
                continue

            try:
                doc_root = ET.fromstring(decoded_xml)
                for dev in doc_root.findall(".//{*}OBEDevicesData"):
                    dev_id = (dev.findtext("{*}GPSDeviceID") or "").strip()
                    if dev_id == imei:
                        found_existing[dev_id] = {
                            "deviceId": dev_id,
                            "locNum": dev.findtext("{*}GeoLocatorNumber", default=""),
                            "locPin": dev.findtext("{*}GeoLocatorPIN", default=""),
                            "status": dev.findtext("{*}GPSDeviceStatus", default=""),
                            "created": dev.findtext("{*}CreationDate", default=""),
                            "info": dev.findtext("{*}AdditionalInformation", default=""),
                            "is_existing": True,
                        }
                        break
                if imei in found_existing:
                    break
            except Exception:
                pass

    return found_existing


def wait_and_fetch_target_response(target_imeis: set, response_xml_path: Path, max_wait_sec: int = 25, poll_interval: int = 3):
    """
    Ожидает появления в очереди PUESC ответа для target_imeis.
    Если устройство уже было зарегистрировано (не вернулось в ZSL_121),
    автоматически запрашивает данные через ZSL_122.
    """
    print(f"\n⏳ Ожидание регистрации/ответа в PUESC для IMEI ({len(target_imeis)}): {', '.join(sorted(target_imeis))}...")

    start_time = time.time()
    found_devices = {}
    found_errors = []

    while time.time() - start_time < max_wait_sec:
        while True:
            filename, decoded_xml = poll_puesc_document()
            if not decoded_xml:
                break

            try:
                doc_root = ET.fromstring(decoded_xml)

                # 1. Проверяем ZSL_121 (новые зарегистрированные)
                for d in doc_root.findall(".//{*}DevicesRegistered"):
                    dev_id = (d.findtext("{*}GPSDeviceID") or "").strip()
                    if dev_id in target_imeis:
                        found_devices[dev_id] = {
                            "deviceId": dev_id,
                            "locNum": d.findtext("{*}GeoLocatorNumber", default=""),
                            "locPin": d.findtext("{*}GeoLocatorPIN", default=""),
                            "status": d.findtext("{*}GPSDeviceStatus", default=""),
                            "created": d.findtext("{*}CreationDate", default=""),
                            "info": d.findtext("{*}AdditionalInformation", default=""),
                            "is_existing": False,
                        }

                # 2. Проверяем ZSL_123 (поисковые ответы)
                for d in doc_root.findall(".//{*}OBEDevicesData"):
                    dev_id = (d.findtext("{*}GPSDeviceID") or "").strip()
                    if dev_id in target_imeis:
                        found_devices[dev_id] = {
                            "deviceId": dev_id,
                            "locNum": d.findtext("{*}GeoLocatorNumber", default=""),
                            "locPin": d.findtext("{*}GeoLocatorPIN", default=""),
                            "status": d.findtext("{*}GPSDeviceStatus", default=""),
                            "created": d.findtext("{*}CreationDate", default=""),
                            "info": d.findtext("{*}AdditionalInformation", default=""),
                            "is_existing": True,
                        }

                # 3. Проверяем ошибки
                for d in doc_root.findall(".//{*}DevicesNotRegistered"):
                    dev_id = (d.findtext("{*}GPSDeviceID") or "").strip()
                    if dev_id in target_imeis:
                        reason = d.findtext("{*}Reason", default="Отклонено PUESC")
                        found_errors.append({"deviceId": dev_id, "reason": reason})

                if len(found_devices) + len(found_errors) >= len(target_imeis):
                    try:
                        with open(response_xml_path, "w", encoding="utf-8") as rf:
                            rf.write(decoded_xml)
                    except Exception:
                        pass
                    return found_devices, found_errors

            except Exception:
                pass

        if len(found_devices) + len(found_errors) >= len(target_imeis):
            return found_devices, found_errors

        elapsed = int(time.time() - start_time)
        print(f"   ⏳ Ожидание генерации ответа PUESC... ({elapsed}/{max_wait_sec} сек.)")
        time.sleep(poll_interval)

    # Если часть устройств не вернулась в ZSL_121 (так как они уже были внесены ранее),
    # опрашиваем PUESC напрямую через ZSL_122
    missing_imeis = target_imeis - set(found_devices.keys()) - {e['deviceId'] for e in found_errors}
    if missing_imeis:
        print(f"\nℹ Устройства {', '.join(missing_imeis)} уже были внесены ранее. Запрос их действующих реквизитов...")
        existing_info = query_existing_devices_info(missing_imeis)
        found_devices.update(existing_info)

    return found_devices, found_errors


def save_results_documents(found_devices: dict, txt_path: Path, csv_path: Path):
    """
    Сохраняет присвоенные PUESC данные (IMEI, Номер локатора, PIN, Дата) в файлы
    по шаблону dd.mm.yyyy_tag_N.txt и dd.mm.yyyy_tag_N.csv.
    """
    if not found_devices:
        return

    # 1. Сохраняем в CSV
# FLESPI CONFIG
FLESPI_TOKEN = "BNwhrmoKyFAqUVKfGbcltAcX3kXAq5fmnxY5SzqduN4Pa0R4BUSVaoKsJOKu8IOJ"
FLESPI_BASE_URL = "https://flespi.io/gw"
FLESPI_GROUP_OVERSEER_SENT_ETOLL = 278416  # "Overseer sent+e-toll"


def sync_overseer_flespi(imei: str):
    """Создает устройство в Flespi и добавляет в группу Overseer sent+e-toll."""
    headers = {
        "Authorization": f"FlespiToken {FLESPI_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    dev_id = None
    # 1. Поиск
    try:
        req = urllib.request.Request(f"{FLESPI_BASE_URL}/devices/all?filter=configuration.ident=={imei}", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("result"):
                dev_id = data["result"][0]["id"]
    except Exception:
        pass

    # 2. Создание
    if not dev_id:
        try:
            payload = [{"name": f"overseer {imei}", "device_type_id": 14, "messages_ttl": 1209600, "configuration": {"ident": imei}}]
            req = urllib.request.Request(f"{FLESPI_BASE_URL}/devices", data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("result"):
                    dev_id = data["result"][0]["id"]
        except Exception:
            pass

    # 3. Добавление в группу
    if dev_id:
        try:
            req = urllib.request.Request(f"{FLESPI_BASE_URL}/groups/{FLESPI_GROUP_OVERSEER_SENT_ETOLL}/devices/{dev_id}", headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return True, dev_id
        except Exception:
            pass
    return False, dev_id


def save_results_documents(found_devices: dict, txt_path: Path, csv_path: Path):
    """Сохраняет полученные данные в CSV и TXT с информацией о Flespi."""
    # 1. Сохраняем в CSV-таблицу
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("IMEI;GeoLocatorNumber;GeoLocatorPIN;Status;CreationDate;AdditionalInformation;FlespiGroup\n")
        for imei, d in found_devices.items():
            f.write(f"{d['deviceId']};{d['locNum']};{d['locPin']};{d['status']};{d['created']};{d['info']};Overseer sent+e-toll\n")

    # 2. Сохраняем в структурированный TXT-документ
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("=" * 110 + "\n")
        f.write(f"ДОКУМЕНТ: РЕЗУЛЬТАТЫ РЕГИСТРАЦИИ УСТРОЙСТВ В PUESC (e-TOLL / SENT) & FLESPI\n")
        f.write(f"Оператор: {OBE_SERVICE_NUMBER} ({OBE_OPERATOR_IDENTITY_NUMBER})\n")
        f.write(f"Дата формирования: {now_str}\n")
        f.write("=" * 110 + "\n")
        f.write(f"{'IMEI':<18} | {'Номер локатора':<20} | {'PIN':<6} | {'Статус':<8} | {'Группа Flespi':<24} | {'Дата'}\n")
        f.write("-" * 110 + "\n")
        for imei, d in found_devices.items():
            f.write(f"{d['deviceId']:<18} | {d['locNum']:<20} | {d['locPin']:<6} | {d['status']:<8} | {'Overseer sent+e-toll':<24} | {d['created']}\n")
        f.write("=" * 110 + "\n")

    print(f"\n📄 Документы с присвоенными данными успешно сформированы:")
    print(f"   ├─ Текстовый документ: {txt_path.name}")
    print(f"   └─ CSV-таблица:        {csv_path.name}")


def main():
    """Основной процесс генерации и отправки в PUESC."""
    print("=" * 70)
    print(f"🚀 Генерация ZSL_120 XML ({PROJECT_TAG}) и отправка в PUESC (e-TOLL / SENT)")
    print("=" * 70)

    # 1. Читаем входной файл
    try:
        with open(INPUT_FILE, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"❌ ОШИБКА: Входной файл '{INPUT_FILE}' не найден.")
        print("Пожалуйста, создайте его и добавьте IMEI (по одному на строку).")
        return

    if not lines:
        print(f"⚠ Входной файл '{INPUT_FILE}' пуст. Завершение работы.")
        return

    print(f"📄 Загружено записей из файла: {len(lines)}")

    # 2. Определяем имя файлов по шаблону dd.mm.yyyy_tag_N
    today_str = datetime.now().strftime("%d.%m.%Y")
    run_idx = get_daily_run_index(BASE_DIR, PROJECT_TAG)
    file_prefix = f"{today_str}_{PROJECT_TAG}_{run_idx}"

    upload_xml_path = BASE_DIR / f"{file_prefix}.xml"
    response_xml_path = BASE_DIR / f"{file_prefix}_response.xml"
    result_txt_path = BASE_DIR / f"{file_prefix}.txt"
    result_csv_path = BASE_DIR / f"{file_prefix}.csv"

    print(f"📝 Порядковый номер запуска сегодня ({today_str}): #{run_idx}")
    print(f"📁 Базовое имя файлов: {file_prefix}.*")

    # 3. Формируем XML
    xml_content, count, target_imeis = create_puesc_xml(lines)

    # 4. Сохраняем локальную копию запроса
    with open(upload_xml_path, "w", encoding="utf-8") as f:
        f.write(xml_content)

    print(f"💾 Локальный XML-файл сохранен: {upload_xml_path.name} (устройств: {count})")

    # 5. Отправляем в PUESC
    print(f"\n📡 Отправка документа в PUESC ({PUESC_URL})...")
    success, sys_ref, _ = send_document_to_puesc(xml_content, filename="ZSL_120.xml")

    if not success:
        print("\n❌ Запрос не был принят PUESC. Работа завершена.")
        return

    print(f"✅ Документ успешно принят PUESC (sysRef: {sys_ref})")

    # 6. Ожидаем ответ именно для наших IMEI (с авто-дозапросом существующих)
    found_devices, found_errors = wait_and_fetch_target_response(target_imeis, response_xml_path, max_wait_sec=20, poll_interval=3)

    print("\n" + "=" * 70)
    print("📊 РЕЗУЛЬТАТ ОБРАБОТКИ В PUESC")
    print("=" * 70)

    if found_devices:
        print(f"\n✅ Получены данные по устройствам: {len(found_devices)} из {len(target_imeis)}")
        for imei, d in found_devices.items():
            source_tag = " (уже было зарегистрировано ранее)" if d.get("is_existing") else " (новая регистрация)"
            print(f"  • IMEI: {d['deviceId']}{source_tag}")
            print(f"    ├─ Номер локатора (ID): {d['locNum']}")
            print(f"    ├─ PIN-код:             {d['locPin']}")
            print(f"    ├─ Статус:              {d['status']}")
            print(f"    └─ Дата создания:       {d['created']}")

        # Сохраняем присвоенные данные в документы TXT и CSV
        save_results_documents(found_devices, result_txt_path, result_csv_path)

        # Синхронизация с группой Flespi (Overseer sent+e-toll)
        print(f"\n📡 Синхронизация устройств с группой Flespi 'Overseer sent+e-toll'...")
        for imei in found_devices.keys():
            ok, dev_id = sync_overseer_flespi(imei)
            status_tag = "✅ добавлено в группу 'Overseer sent+e-toll'" if ok else "⚠ не удалось добавить"
            print(f"  • IMEI {imei} (Flespi ID: {dev_id}) -> {status_tag}")

    if found_errors:
        print(f"\n❌ Ошибки регистрации устройств: {len(found_errors)}")
        for err in found_errors:
            print(f"  • IMEI: {err['deviceId']} — Причина: {err['reason']}")

    missing = target_imeis - set(found_devices.keys()) - {e['deviceId'] for e in found_errors}
    if missing:
        print(f"\n⏳ Не удалось получить данные по устройствам:")
        for imei in missing:
            print(f"  • IMEI: {imei}")

    print("\n🏁 Работа скрипта завершена.")


if __name__ == "__main__":
    main()