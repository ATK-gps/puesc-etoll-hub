# -*- coding: utf-8 -*-
import os
import ssl
import time
import uuid
import base64
import hashlib
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

from aiohttp import web

# ==============================================================================
# --- НАСТРОЙКИ PUESC & FLESPI ---
# ==============================================================================

PUESC_URL = "https://ws.puesc.gov.pl/seap_wsChannel/DocumentHandlingPort"
PUESC_LOGIN = "zam@atk-gps.by"
PUESC_PASSWORD = "HFxqCHa6eC-+47x"

OBE_SERVICE_NUMBER = "ZSL-GRNC-0"
OBE_OPERATOR_IDENTITY_TYPE = "INNY"
OBE_OPERATOR_IDENTITY_NUMBER = "BY291080284"

RESPONSE_URL = "https://device-etoll.atkgps.pl/DocumentHandlingSvc"
RESPONSE_USERNAME = "zam@atk-gps.by"
RESPONSE_PASSWORD = "HFxqCHa6eC-+47x"
RESPONSE_CERT_FINGERPRINT = "91f371717516763d8fd3d60091877e23059d90ce"

# Flespi API
FLESPI_TOKEN = "BNwhrmoKyFAqUVKfGbcltAcX3kXAq5fmnxY5SzqduN4Pa0R4BUSVaoKsJOKu8IOJ"
FLESPI_BASE_URL = "https://flespi.io/gw"

FLESPI_GROUPS = {
    "motoguard_etoll": 247707,         # MotoGuard e-toll
    "overseer_etoll": 278414,          # Overseer e-toll
    "overseer_sent": 278415,           # Overseer sent
    "overseer_sent_etoll": 278416,     # Overseer sent+e-toll
    "motoguard_to_delete": 299554,     # MotoGuard to delete
    "overseer_to_delete": 305528,      # Overseer to delete
}

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
STATIC_DIR = BASE_DIR / "static"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# ==============================================================================


def get_ssl_context():
    """Формирует SSL-контекст с защитой от ошибок на macOS/серверах."""
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
    """Определяет следующий номер запуска за сегодня (dd.mm.yyyy_tag_N)."""
    today_str = datetime.now().strftime("%d.%m.%Y")
    max_idx = 0
    clean_tag = tag.strip().lower().replace(" ", "_")
    pattern = f"{today_str}_{clean_tag}_"

    if target_dir.exists():
        for item in target_dir.iterdir():
            if item.is_file() and item.name.startswith(pattern):
                remainder = item.name[len(pattern):]
                part = remainder.split(".")[0].split("_")[0]
                if part.isdigit():
                    max_idx = max(max_idx, int(part))

    return max_idx + 1


# ==============================================================================
# --- FLESPI ИНТЕГРАЦИЯ ---
# ==============================================================================

def get_or_create_flespi_device(imei: str, contractor: str):
    """
    Находит существующее устройство в Flespi по IMEI или создает новое.
    Возвращает: (device_id, device_name)
    """
    clean_imei = str(imei).strip()
    if len(clean_imei) < 6:
        return None, None

    clean_c = contractor.strip().lower()
    headers = {
        "Authorization": f"FlespiToken {FLESPI_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # 1. Поиск по ident (IMEI) через селектор Flespi
    import urllib.parse
    selector = urllib.parse.quote(f'{{configuration.ident=="{clean_imei}"}}')
    search_url = f"{FLESPI_BASE_URL}/devices/{selector}"
    req = urllib.request.Request(search_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10, context=get_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("result", [])
            if results:
                return results[0]["id"], results[0].get("name", "")
    except Exception:
        pass

    # 2. Создание, если устройства нет
    if "overseer" in clean_c:
        name = f"overseer {clean_imei}"
        dev_type = 14
        config = {"ident": clean_imei}
    else:
        name = f"Motoguard {clean_imei}"
        dev_type = 22
        config = {"ident": clean_imei, "settings_polling": "once"}

    payload = [{
        "name": name,
        "device_type_id": dev_type,
        "messages_ttl": 1209600,
        "configuration": config,
    }]

    create_req = urllib.request.Request(
        f"{FLESPI_BASE_URL}/devices",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    try:
        with urllib.request.urlopen(create_req, timeout=10, context=get_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("result", [])
            if results:
                return results[0]["id"], results[0].get("name", "")
    except Exception:
        pass

    return None, None


def add_device_to_flespi_group(group_id: int, device_id: int):
    """Добавляет устройство в группу Flespi."""
    if not group_id or not device_id:
        return False
    headers = {
        "Authorization": f"FlespiToken {FLESPI_TOKEN}",
        "Accept": "application/json",
    }
    url = f"{FLESPI_BASE_URL}/groups/{group_id}/devices/{device_id}"
    req = urllib.request.Request(url, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10, context=get_ssl_context()) as resp:
            return True
    except Exception:
        return False


def determine_flespi_target_group(contractor: str, etoll: bool, sent: bool):
    """
    Определяет группу Flespi по правилам:
    - overseer + (sent & etoll) -> 278416 (Overseer sent+e-toll)
    - overseer + etoll (без sent) -> 278414 (Overseer e-toll)
    - overseer + sent (без etoll) -> 278415 (Overseer sent)
    - motoguard + etoll -> 247707 (MotoGuard e-toll)
    """
    clean_c = contractor.strip().lower()
    if "overseer" in clean_c:
        if sent and etoll:
            return FLESPI_GROUPS["overseer_sent_etoll"], "Overseer sent+e-toll"
        elif etoll:
            return FLESPI_GROUPS["overseer_etoll"], "Overseer e-toll"
        elif sent:
            return FLESPI_GROUPS["overseer_sent"], "Overseer sent"
    elif "motoguard" in clean_c or "moto" in clean_c:
        if etoll:
            return FLESPI_GROUPS["motoguard_etoll"], "MotoGuard e-toll"

    # Default fallback for ATK-GPS / others:
    if "overseer" in clean_c or "sent" in clean_c:
        return FLESPI_GROUPS["overseer_sent_etoll"], "Overseer sent+e-toll"
    return FLESPI_GROUPS["motoguard_etoll"], "MotoGuard e-toll"


def sync_device_with_flespi(imei: str, contractor: str, etoll: bool, sent: bool):
    """Синхронизирует устройство с Flespi и добавляет в нужную группу."""
    clean_imei = str(imei).strip()
    if len(clean_imei) < 6:
        return False, "", None
    group_id, group_name = determine_flespi_target_group(contractor, etoll, sent)
    dev_id, dev_name = get_or_create_flespi_device(clean_imei, contractor)
    if dev_id and group_id:
        success = add_device_to_flespi_group(group_id, dev_id)
        if success:
            return True, group_name, dev_id
    return False, "", dev_id


def delete_flespi_devices(imeis_list: list):
    """Массово удаляет устройства из Flespi по списку IMEI."""
    clean_imeis = [str(i).strip() for i in imeis_list if str(i).strip()]
    if not clean_imeis:
        return 0, 0

    import urllib.parse
    headers = {
        "Authorization": f"FlespiToken {FLESPI_TOKEN}",
        "Accept": "application/json",
    }
    expression = " || ".join([f'configuration.ident=="{imei}"' for imei in clean_imeis])
    selector = f"{{{expression}}}"
    encoded_selector = urllib.parse.quote(selector)
    url = f"{FLESPI_BASE_URL}/devices/{encoded_selector}"

    req = urllib.request.Request(url, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=20, context=get_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            deleted_count = len(data.get("result", []))
            errors_count = len(data.get("errors", []))
            return deleted_count, errors_count
    except Exception:
        return 0, len(clean_imeis)


def fetch_and_convert_motoguard_activity():
    """
    Запрашивает из Flespi устройства MotoGuard (name, last_active),
    конвертирует время (timestamp -> readable UTC / Nigdy) по правилам motoguard_convert.py
    и сохраняет структурированные файлы JSON, CSV, TXT.
    """
    import urllib.parse
    headers = {
        "Authorization": f"FlespiToken {FLESPI_TOKEN}",
        "Accept": "application/json",
    }
    selector = urllib.parse.quote('{name~"*motoguard*"}')
    url = f"{FLESPI_BASE_URL}/devices/{selector}?fields=name,last_active"
    req = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(req, timeout=25, context=get_ssl_context()) as resp:
        raw_data = json.loads(resp.read().decode("utf-8"))
        source_list = raw_data.get("result", [])

    processed_list = []
    active_count = 0
    never_count = 0

    for item in source_list:
        name = item.get("name", "Unknown")
        timestamp = item.get("last_active", 0)
        if not timestamp or timestamp == 0:
            readable_time = "Nigdy"
            never_count += 1
        else:
            readable_time = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            active_count += 1
        processed_list.append({
            "name": name,
            "last_active": readable_time,
        })

    today_str = datetime.now().strftime("%d.%m.%Y")
    run_idx = get_daily_run_index(REPORTS_DIR, "motoguard_activity")
    file_prefix = f"{today_str}_motoguard_activity_{run_idx}"

    json_path = REPORTS_DIR / f"{file_prefix}.json"
    csv_path = REPORTS_DIR / f"{file_prefix}.csv"
    txt_path = REPORTS_DIR / f"{file_prefix}.txt"

    # 1. JSON
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"result": processed_list}, f, indent=2, ensure_ascii=False)

    # 2. CSV
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("Name;LastActive\n")
        for item in processed_list:
            f.write(f"{item['name']};{item['last_active']}\n")

    # 3. TXT
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("=" * 70 + "\n")
        f.write("ОТЧЕТ: АКТИВНОСТЬ УСТРОЙСТВ MOTOGUARD В ПЛАТФОРМЕ FLESPI\n")
        f.write(f"Дата формирования: {now_str}\n")
        f.write(f"Всего устройств: {len(processed_list)} (Активны: {active_count}, Никогда: {never_count})\n")
        f.write("=" * 70 + "\n")
        f.write(f"{'Название устройства':<40} | {'Последняя активность'}\n")
        f.write("-" * 70 + "\n")
        for item in processed_list:
            f.write(f"{item['name']:<40} | {item['last_active']}\n")
        f.write("=" * 70 + "\n")

    return {
        "success": True,
        "count": len(processed_list),
        "active_count": active_count,
        "never_count": never_count,
        "devices": processed_list,
        "files": {
            "json": json_path.name,
            "csv": csv_path.name,
            "txt": txt_path.name,
        }
    }


# ==============================================================================
# --- PUESC SOAP МЕТОДЫ ---
# ==============================================================================

def create_ws_security_credentials(password: str):
    """Формирует параметры WS-Security UsernameToken (PasswordDigest)."""
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


def create_puesc_registration_xml(devices: list):
    """
    Формирует XML ZSL_120 для добавления устройств.
    """
    req_uid = uuid.uuid4().hex
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- RequestID: {req_uid} -->
<ns1:ZSL_120 xmlns:ns1="http://www.mf.gov.pl/SENT/2020/07/21/ZSL_120.xsd" xmlns:ns2="http://www.mf.gov.pl/SENT/2020/07/21/ZTypes.xsd">
    <ns1:OBEServiceNumber>{OBE_SERVICE_NUMBER}</ns1:OBEServiceNumber>
    <ns1:OBEOperatorIdentityType>{OBE_OPERATOR_IDENTITY_TYPE}</ns1:OBEOperatorIdentityType>
    <ns1:OBEOperatorIdentityNumber>{OBE_OPERATOR_IDENTITY_NUMBER}</ns1:OBEOperatorIdentityNumber>
"""
    for d in devices:
        imei = str(d.get("imei", "")).strip()
        info = str(d.get("info", "")).strip() or f"Device {imei}"
        xml_content += f"""    <ns1:GPSDeviceToAdd>
        <ns2:GPSDeviceID>{imei}</ns2:GPSDeviceID>
        <ns2:AdditionalInformation>{info}</ns2:AdditionalInformation>
    </ns1:GPSDeviceToAdd>
"""

    xml_content += f"""    <ns1:ResponseWebService>
        <ns2:UrlAddress>{RESPONSE_URL}</ns2:UrlAddress>
        <ns2:UserName>{RESPONSE_USERNAME}</ns2:UserName>
        <ns2:UserPassword>{RESPONSE_PASSWORD}</ns2:UserPassword>
        <ns2:CertificateFingerPrint>{RESPONSE_CERT_FINGERPRINT}</ns2:CertificateFingerPrint>
    </ns1:ResponseWebService>
</ns1:ZSL_120>
"""
    return xml_content


def create_puesc_deletion_xml(imeis: list):
    """Формирует XML ZSL_120 для удаления устройств."""
    req_uid = uuid.uuid4().hex
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- RequestID: {req_uid} -->
<ns1:ZSL_120 xmlns:ns1="http://www.mf.gov.pl/SENT/2020/07/21/ZSL_120.xsd" xmlns:ns2="http://www.mf.gov.pl/SENT/2020/07/21/ZTypes.xsd">
    <ns1:OBEServiceNumber>{OBE_SERVICE_NUMBER}</ns1:OBEServiceNumber>
    <ns1:OBEOperatorIdentityType>{OBE_OPERATOR_IDENTITY_TYPE}</ns1:OBEOperatorIdentityType>
    <ns1:OBEOperatorIdentityNumber>{OBE_OPERATOR_IDENTITY_NUMBER}</ns1:OBEOperatorIdentityNumber>
"""
    for imei in imeis:
        clean_imei = str(imei).strip()
        if clean_imei:
            xml_content += f"    <ns1:GPSDeviceToRemove>{clean_imei}</ns1:GPSDeviceToRemove>\n"

    xml_content += f"""    <ns1:ResponseWebService>
        <ns2:UrlAddress>{RESPONSE_URL}</ns2:UrlAddress>
        <ns2:UserName>{RESPONSE_USERNAME}</ns2:UserName>
        <ns2:UserPassword>{RESPONSE_PASSWORD}</ns2:UserPassword>
        <ns2:CertificateFingerPrint>{RESPONSE_CERT_FINGERPRINT}</ns2:CertificateFingerPrint>
    </ns1:ResponseWebService>
</ns1:ZSL_120>
"""
    return xml_content


def create_zsl122_query_xml(imei: str):
    """Формирует XML ZSL_122 для поиска/проверки статуса устройства."""
    req_uid = uuid.uuid4().hex
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- RequestID: {req_uid} -->
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


def send_soap_to_puesc(xml_content: str, filename: str = "document.xml"):
    """Синхронная отправка SOAP AcceptDocumentRequest в PUESC."""
    try:
        # Обеспечиваем уникальность имени файла, чтобы PUESC не отвергал дубликаты
        base_name = filename.rsplit(".", 1)[0]
        ext = filename.rsplit(".", 1)[1] if "." in filename else "xml"
        unique_fn = f"{base_name}_{uuid.uuid4().hex[:8]}.{ext}"

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
                <_v21:content filename='{unique_fn}' mime="application/xml">{content_b64}</_v21:content>
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

        with urllib.request.urlopen(req, timeout=20, context=ssl_ctx) as resp:
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


def poll_single_document_from_queue():
    """Вычитывает один входящий документ из очереди GetNextDocument."""
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

        with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
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


def parse_obe_devices_data(doc_root, target_imeis: set = None):
    """
    Разбирает ZSL_123 (OBEDevicesData), группирует записи по IMEI и для каждого
    выбирает наиболее актуальную запись:
    1. Приоритет: активная запись (GPSDeviceStatus == '0') с самой свежей датой.
    2. Если активных нет — берет самую свежую запись по дате изменения (например, статус 4 - удален).
    """
    grouped = {}
    for d in doc_root.findall(".//{*}OBEDevicesData"):
        dev_id = (d.findtext("{*}GPSDeviceID") or "").strip()
        if not dev_id:
            continue
        if target_imeis and dev_id not in target_imeis:
            continue

        item = {
            "deviceId": dev_id,
            "locNum": d.findtext("{*}GeoLocatorNumber", default="") or "-",
            "locPin": d.findtext("{*}GeoLocatorPIN", default="") or "-",
            "status": d.findtext("{*}GPSDeviceStatus", default="") or "0",
            "created": d.findtext("{*}CreationDate") or "",
            "modified": d.findtext("{*}ModificationDate") or "",
            "info": d.findtext("{*}AdditionalInformation", default=""),
        }
        grouped.setdefault(dev_id, []).append(item)

    best_results = {}
    for dev_id, records in grouped.items():
        def sort_key(r):
            is_active = 1 if str(r["status"]).strip() == "0" else 0
            date_str = r["modified"] or r["created"] or ""
            return (is_active, date_str)

        sorted_records = sorted(records, key=sort_key, reverse=True)
        best = sorted_records[0]
        best_results[dev_id] = {
            "deviceId": best["deviceId"],
            "locNum": best["locNum"],
            "locPin": best["locPin"],
            "status": best["status"],
            "created": best["created"] or best["modified"],
            "info": best["info"],
            "is_existing": (str(best["status"]).strip() == "0"),
        }
    return best_results


def query_single_device_info(imei: str, max_wait_sec: int = 10):
    """Поиск данных конкретного устройства через ZSL_122 с точным выбором активной записи."""
    clean_imei = str(imei).strip()
    if len(clean_imei) < 6:
        return None

    zsl122_xml = create_zsl122_query_xml(clean_imei)
    success, sys_ref, _ = send_soap_to_puesc(zsl122_xml, filename="ZSL_122.xml")
    if not success:
        return None

    start_t = time.time()
    while time.time() - start_t < max_wait_sec:
        time.sleep(1)
        fn, decoded_xml = poll_single_document_from_queue()
        if not decoded_xml:
            continue

        try:
            doc_root = ET.fromstring(decoded_xml)
            parsed = parse_obe_devices_data(doc_root, {clean_imei})
            if clean_imei in parsed:
                return parsed[clean_imei]
        except Exception:
            pass

    return None


async def process_registration_request(contractor: str, devices_data: list):
    """
    Асинхронный процесс регистрации списка устройств в PUESC + Синхронизация с Flespi группами.
    """
    target_imeis = {str(d["imei"]).strip() for d in devices_data if str(d.get("imei", "")).strip()}
    today_str = datetime.now().strftime("%d.%m.%Y")
    clean_tag = contractor.strip().lower().replace(" ", "_") if contractor.strip() else "devices"
    run_idx = get_daily_run_index(REPORTS_DIR, clean_tag)
    file_prefix = f"{today_str}_{clean_tag}_{run_idx}"

    upload_xml_path = REPORTS_DIR / f"{file_prefix}.xml"
    response_xml_path = REPORTS_DIR / f"{file_prefix}_response.xml"
    result_txt_path = REPORTS_DIR / f"{file_prefix}.txt"
    result_csv_path = REPORTS_DIR / f"{file_prefix}.csv"

    # 1. Формируем и сохраняем XML
    xml_content = create_puesc_registration_xml(devices_data)
    with open(upload_xml_path, "w", encoding="utf-8") as f:
        f.write(xml_content)

    # 2. Отправляем в PUESC
    loop = asyncio.get_event_loop()
    success, sys_ref, err_msg = await loop.run_in_executor(None, send_soap_to_puesc, xml_content, "ZSL_120.xml")

    if not success:
        return {
            "success": False,
            "error": f"PUESC отклонил запрос: {err_msg}",
            "sysRef": None,
            "results": [],
        }

    # 3. Ожидаем ответ ZSL_121 от PUESC
    found_devices = {}
    found_errors = []
    raw_response_xml = None
    start_t = time.time()
    max_wait = 16

    while time.time() - start_t < max_wait:
        while True:
            fn, decoded_xml = await loop.run_in_executor(None, poll_single_document_from_queue)
            if not decoded_xml:
                break

            try:
                doc_root = ET.fromstring(decoded_xml)
                
                # 3.1 Ответ ZSL_121 - УСПЕШНАЯ РЕГИСТРАЦИЯ
                for d in doc_root.findall(".//{*}DevicesRegistered"):
                    dev_id = (d.findtext("{*}GPSDeviceID") or "").strip()
                    if dev_id in target_imeis:
                        found_devices[dev_id] = {
                            "deviceId": dev_id,
                            "locNum": d.findtext("{*}GeoLocatorNumber", default=""),
                            "locPin": d.findtext("{*}GeoLocatorPIN", default=""),
                            "status": d.findtext("{*}GPSDeviceStatus", default="0"),
                            "created": d.findtext("{*}CreationDate", default=""),
                            "info": d.findtext("{*}AdditionalInformation", default=""),
                            "is_existing": False,
                        }

                # 3.2 Ответ ZSL_121 - ОШИБКИ / ОТКАЗЫ
                for d in doc_root.findall(".//{*}DevicesNotRegistered"):
                    dev_id = (d.findtext("{*}GPSDeviceID") or "").strip()
                    if dev_id in target_imeis:
                        reason = d.findtext("{*}Reason", default="Отклонено PUESC")
                        found_errors.append({"deviceId": dev_id, "reason": reason})

                if len(found_devices) + len(found_errors) >= len(target_imeis):
                    raw_response_xml = decoded_xml
                    break
            except Exception:
                pass

        if len(found_devices) + len(found_errors) >= len(target_imeis):
            break
        await asyncio.sleep(1.5)

    # 4. Для всех устройств, которые не зарегистрированы как новые (включая "уже в базе"),
    # запрашиваем актуальный активный статус в PUESC через ZSL_122
    check_imeis = [m for m in (target_imeis - set(found_devices.keys())) if len(m) >= 6]
    for m_imei in check_imeis:
        existing_info = await loop.run_in_executor(None, query_single_device_info, m_imei, 10)
        if existing_info and str(existing_info.get("status", "")).strip() == "0":
            found_devices[m_imei] = existing_info
            # Убираем из ошибок, так как устройство найдено активным в базе PUESC
            found_errors = [e for e in found_errors if e.get("deviceId") != m_imei]

    # 5. Сохраняем сырой ответ
    if raw_response_xml:
        with open(response_xml_path, "w", encoding="utf-8") as f:
            f.write(raw_response_xml)

    # 6. Синхронизация с FLESPI ГРУППАМИ
    flespi_status_map = {}
    req_map = {str(d["imei"]).strip(): d for d in devices_data}

    for imei in target_imeis:
        src = req_map.get(imei, {})
        etoll_flag = src.get("etoll", True)
        sent_flag = src.get("sent", True)

        flespi_ok, flespi_grp, dev_id = await loop.run_in_executor(
            None, sync_device_with_flespi, imei, contractor, etoll_flag, sent_flag
        )
        flespi_status_map[imei] = {
            "group": flespi_grp,
            "synced": flespi_ok,
            "flespi_id": dev_id,
        }

    # 7. Формируем отчеты TXT и CSV
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # CSV
    with open(result_csv_path, "w", encoding="utf-8") as f:
        f.write("IMEI;GeoLocatorNumber;GeoLocatorPIN;Status;CreationDate;AdditionalInformation;FlespiGroup;Type\n")
        for imei, d in found_devices.items():
            reg_type = "EXISTING" if d.get("is_existing") else "NEW"
            f_grp = flespi_status_map.get(imei, {}).get("group", "")
            f.write(f"{d['deviceId']};{d['locNum']};{d['locPin']};{d['status']};{d['created']};{d['info']};{f_grp};{reg_type}\n")
        for e in found_errors:
            f_grp = flespi_status_map.get(e['deviceId'], {}).get("group", "")
            f.write(f"{e['deviceId']};;;;;{f_grp};ERROR: {e['reason']}\n")

    # TXT
    with open(result_txt_path, "w", encoding="utf-8") as f:
        f.write("=" * 110 + "\n")
        f.write("ДОКУМЕНТ: РЕЗУЛЬТАТЫ РЕГИСТРАЦИИ УСТРОЙСТВ В PUESC (e-TOLL / SENT) & FLESPI\n")
        f.write(f"Контрагент: {contractor or 'Общий'}\n")
        f.write(f"Оператор: {OBE_SERVICE_NUMBER} ({OBE_OPERATOR_IDENTITY_NUMBER})\n")
        f.write(f"Дата формирования: {now_str}\n")
        f.write("=" * 110 + "\n")
        f.write(f"{'IMEI':<18} | {'Номер локатора':<18} | {'PIN':<6} | {'Статус':<8} | {'Группа Flespi':<24} | {'Примечание'}\n")
        f.write("-" * 110 + "\n")
        for imei, d in found_devices.items():
            note = "(ранее в базе)" if d.get("is_existing") else "(новый)"
            f_grp = flespi_status_map.get(imei, {}).get("group", "-")
            f.write(f"{d['deviceId']:<18} | {d['locNum']:<18} | {d['locPin']:<6} | {d['status']:<8} | {f_grp:<24} | {d['created']} {note}\n")
        for e in found_errors:
            f_grp = flespi_status_map.get(e['deviceId'], {}).get("group", "-")
            f.write(f"{e['deviceId']:<18} | {'-':<18} | {'-':<6} | {'ОШИБКА':<8} | {f_grp:<24} | {e['reason']}\n")
        f.write("=" * 110 + "\n")

    # Преобразуем в структурированный список результатов
    results_list = []
    for imei in target_imeis:
        src = req_map.get(imei, {})
        f_info = flespi_status_map.get(imei, {})
        if imei in found_devices:
            d = found_devices[imei]
            results_list.append({
                "imei": imei,
                "info": d.get("info") or src.get("info", ""),
                "etoll": src.get("etoll", True),
                "sent": src.get("sent", True),
                "locNum": d.get("locNum", ""),
                "locPin": d.get("locPin", ""),
                "status": d.get("status", "0"),
                "created": d.get("created", ""),
                "is_existing": d.get("is_existing", False),
                "flespi_group": f_info.get("group", ""),
                "flespi_synced": f_info.get("synced", False),
                "success": True,
                "error": None,
            })
        else:
            err = next((e["reason"] for e in found_errors if e["deviceId"] == imei), "Не зарегистрировано в PUESC (некорректный IMEI или отклонен)")
            results_list.append({
                "imei": imei,
                "info": src.get("info", ""),
                "etoll": src.get("etoll", True),
                "sent": src.get("sent", True),
                "locNum": "-",
                "locPin": "-",
                "status": "-",
                "created": "-",
                "is_existing": False,
                "flespi_group": f_info.get("group", ""),
                "flespi_synced": f_info.get("synced", False),
                "success": False,
                "error": err,
            })

    return {
        "success": True,
        "sysRef": sys_ref,
        "prefix": file_prefix,
        "results": results_list,
        "files": {
            "txt": result_txt_path.name,
            "csv": result_csv_path.name,
            "xml": upload_xml_path.name,
            "response_xml": response_xml_path.name if response_xml_path.exists() else None,
        },
    }


async def process_deletion_request(imeis_list: list):
    """
    Асинхронный процесс удаления устройств из PUESC.
    """
    today_str = datetime.now().strftime("%d.%m.%Y")
    run_idx = get_daily_run_index(REPORTS_DIR, "delete")
    file_prefix = f"{today_str}_delete_{run_idx}"

    upload_xml_path = REPORTS_DIR / f"{file_prefix}.xml"
    result_txt_path = REPORTS_DIR / f"{file_prefix}.txt"
    result_csv_path = REPORTS_DIR / f"{file_prefix}.csv"

    # 1. Формируем XML
    xml_content = create_puesc_deletion_xml(imeis_list)
    with open(upload_xml_path, "w", encoding="utf-8") as f:
        f.write(xml_content)

    loop = asyncio.get_event_loop()
    success, sys_ref, err_msg = await loop.run_in_executor(None, send_soap_to_puesc, xml_content, "ZSL_120_delete.xml")

    if not success:
        return {
            "success": False,
            "error": f"Ошибка отправки удаления: {err_msg}",
            "results": [],
        }

    # 2. Ожидаем ответ ZSL_121 (DevicesRemoved)
    target_set = {str(i).strip() for i in imeis_list if str(i).strip()}
    removed_now = {}
    failed_devices = []
    start_t = time.time()

    while time.time() - start_t < 12:
        while True:
            fn, decoded_xml = await loop.run_in_executor(None, poll_single_document_from_queue)
            if not decoded_xml:
                break
            try:
                doc_root = ET.fromstring(decoded_xml)
                for d in doc_root.findall(".//{*}DevicesRemoved"):
                    dev_id = (d.findtext("{*}GPSDeviceID") or "").strip()
                    if dev_id in target_set:
                        removed_now[dev_id] = {
                            "deviceId": dev_id,
                            "locNum": d.findtext("{*}GeoLocatorNumber", default=""),
                            "status": "4",
                            "modDate": d.findtext("{*}ModificationDate", default=""),
                        }
                for d in doc_root.findall(".//{*}DevicesFailed"):
                    dev_id = (d.findtext("{*}GPSDeviceID") or "").strip()
                    if dev_id in target_set:
                        reason = d.findtext("{*}Reason", default="Ошибка удаления")
                        failed_devices.append({"deviceId": dev_id, "reason": reason})
            except Exception:
                pass
        if len(removed_now) + len(failed_devices) >= len(target_set):
            break
        await asyncio.sleep(1.5)

    # 3. Удаляем объекты из платформы Flespi
    flespi_deleted, flespi_errors = await loop.run_in_executor(None, delete_flespi_devices, imeis_list)

    # 4. Сохраняем отчеты
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    results_list = []

    for imei in target_set:
        if imei in removed_now:
            loc = removed_now[imei]["locNum"]
        else:
            # Пытаемся получить последний номер локатора из PUESC
            existing = await loop.run_in_executor(None, query_single_device_info, imei, 3)
            loc = existing.get("locNum", "-") if existing else "-"

        results_list.append({
            "imei": imei,
            "status": "4 (Удален)",
            "result": "Деактивировано в PUESC и удалено из Flespi",
            "locNum": loc,
            "flespi_status": "Удалено из Flespi",
            "success": True,
        })

    with open(result_csv_path, "w", encoding="utf-8") as f:
        f.write("IMEI;GeoLocatorNumber;Status;ModificationDate;Result;FlespiStatus\n")
        for r in results_list:
            f.write(f"{r['imei']};{r['locNum']};4;{now_str};DEACTIVATED;REMOVED_FROM_FLESPI\n")

    with open(result_txt_path, "w", encoding="utf-8") as f:
        f.write("=" * 95 + "\n")
        f.write("ДОКУМЕНТ: РЕЗУЛЬТАТЫ УДАЛЕНИЯ УСТРОЙСТВ ИЗ PUESC (e-TOLL) & FLESPI\n")
        f.write(f"Оператор: {OBE_SERVICE_NUMBER} ({OBE_OPERATOR_IDENTITY_NUMBER})\n")
        f.write(f"Дата формирования: {now_str}\n")
        f.write(f"Удалено объектов из Flespi: {flespi_deleted}\n")
        f.write("=" * 95 + "\n")
        f.write(f"{'IMEI':<18} | {'Номер локатора':<22} | {'Статус':<12} | {'Результат'}\n")
        f.write("-" * 95 + "\n")
        for r in results_list:
            f.write(f"{r['imei']:<18} | {r['locNum']:<22} | {'4 Удален':<12} | {r['result']}\n")
        f.write("=" * 95 + "\n")

    return {
        "success": True,
        "sysRef": sys_ref,
        "prefix": file_prefix,
        "results": results_list,
        "files": {
            "txt": result_txt_path.name,
            "csv": result_csv_path.name,
            "xml": upload_xml_path.name,
        },
    }


# ==============================================================================
# --- HTTP ROUTERS & API ---
# ==============================================================================

@web.middleware
async def cors_middleware(request, handler):
    """CORS middleware для предотвращения любых блокировок в браузерах."""
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PUT, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    return response


async def handle_index(request):
    """Отдает главную страницу SPA."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return web.FileResponse(index_file)
    return web.Response(text="Web UI Loading...", content_type="text/html")


async def handle_api_register(request):
    """API эндпоинт для регистрации устройств."""
    try:
        data = await request.json()
        contractor = data.get("contractor", "").strip()
        devices = data.get("devices", [])

        if not devices:
            return web.json_response({"success": False, "error": "Список устройств пуст."}, status=400)

        res = await process_registration_request(contractor, devices)
        return web.json_response(res)
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_api_delete(request):
    """API эндпоинт для удаления устройств."""
    try:
        data = await request.json()
        imeis = data.get("imeis", [])

        if not imeis:
            return web.json_response({"success": False, "error": "Список IMEI пуст."}, status=400)

        res = await process_deletion_request(imeis)
        return web.json_response(res)
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_api_status(request):
    """API эндпоинт для запроса статуса устройства."""
    try:
        data = await request.json()
        imei = str(data.get("imei", "")).strip()

        if not imei:
            return web.json_response({"success": False, "error": "IMEI не указан."}, status=400)

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, query_single_device_info, imei, 6)

        if info:
            return web.json_response({"success": True, "device": info})
        else:
            return web.json_response({"success": False, "error": "Устройство не найдено в PUESC."})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_api_motoguard_activity(request):
    """API эндпоинт для запроса и конвертации активности MotoGuard из Flespi."""
    try:
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, fetch_and_convert_motoguard_activity)
        return web.json_response(res)
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_api_download(request):
    """Скачивание сформированного файла отчета."""
    filename = request.match_info.get("filename")
    file_path = REPORTS_DIR / filename

    if file_path.exists() and file_path.is_file():
        return web.FileResponse(file_path)
    return web.Response(text="Файл не найден", status=404)


async def handle_api_history(request):
    """Список недавних файлов в папке reports."""
    files = []
    if REPORTS_DIR.exists():
        for f in sorted(REPORTS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and not f.name.startswith("."):
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d.%m.%Y %H:%M:%S"),
                })
    return web.json_response({"files": files[:30]})


def init_app():
    """Инициализация приложения aiohttp с CORS."""
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", handle_index)
    app.router.add_post("/api/register", handle_api_register)
    app.router.add_post("/api/delete", handle_api_delete)
    app.router.add_post("/api/status", handle_api_status)
    app.router.add_get("/api/flespi/motoguard_activity", handle_api_motoguard_activity)
    app.router.add_post("/api/flespi/motoguard_activity", handle_api_motoguard_activity)
    app.router.add_get("/api/download/{filename}", handle_api_download)
    app.router.add_get("/api/history", handle_api_history)
    app.router.add_static("/static/", path=str(STATIC_DIR), name="static")
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print("=" * 70)
    print(f"🚀 PUESC & Flespi Web Service запущен на http://localhost:{port}")
    print(f"📁 Папка отчетов: {REPORTS_DIR}")
    print("=" * 70)
    web.run_app(init_app(), host="0.0.0.0", port=port)
