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
# --- НАСТРОЙКИ PUESC ---
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
    devices: list of dict {'imei': str, 'info': str, 'etoll': bool, 'sent': bool}
    """
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
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
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
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


def send_soap_to_puesc(xml_content: str, filename: str = "document.xml"):
    """Синхронная отправка SOAP AcceptDocumentRequest в PUESC."""
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


def query_single_device_info(imei: str, max_wait_sec: int = 4):
    """Поиск данных конкретного устройства через ZSL_122 с коротким таймаутом."""
    if len(str(imei).strip()) < 6:
        return None

    zsl122_xml = create_zsl122_query_xml(imei)
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
            for dev in doc_root.findall(".//{*}OBEDevicesData"):
                dev_id = (dev.findtext("{*}GPSDeviceID") or "").strip()
                if dev_id == imei:
                    return {
                        "deviceId": dev_id,
                        "locNum": dev.findtext("{*}GeoLocatorNumber", default=""),
                        "locPin": dev.findtext("{*}GeoLocatorPIN", default=""),
                        "status": dev.findtext("{*}GPSDeviceStatus", default=""),
                        "created": dev.findtext("{*}CreationDate") or dev.findtext("{*}ModificationDate") or "",
                        "info": dev.findtext("{*}AdditionalInformation", default=""),
                        "is_existing": True,
                    }
        except Exception:
            pass

    return None


async def process_registration_request(contractor: str, devices_data: list):
    """
    Асинхронный процесс регистрации списка устройств в PUESC.
    Оптимизирован для предотвращения таймаутов браузера/Cloudflare.
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

    # 3. Ожидаем ответ ZSL_121
    found_devices = {}
    found_errors = []
    raw_response_xml = None
    start_t = time.time()
    max_wait = 14

    while time.time() - start_t < max_wait:
        while True:
            fn, decoded_xml = await loop.run_in_executor(None, poll_single_document_from_queue)
            if not decoded_xml:
                break

            try:
                doc_root = ET.fromstring(decoded_xml)
                # ZSL_121
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

                # ZSL_123
                for d in doc_root.findall(".//{*}OBEDevicesData"):
                    dev_id = (d.findtext("{*}GPSDeviceID") or "").strip()
                    if dev_id in target_imeis:
                        found_devices[dev_id] = {
                            "deviceId": dev_id,
                            "locNum": d.findtext("{*}GeoLocatorNumber", default=""),
                            "locPin": d.findtext("{*}GeoLocatorPIN", default=""),
                            "status": d.findtext("{*}GPSDeviceStatus", default="0"),
                            "created": d.findtext("{*}CreationDate", default=""),
                            "info": d.findtext("{*}AdditionalInformation", default=""),
                            "is_existing": True,
                        }

                # Errors
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

    # 4. Если устройство уже было в базе, запрашиваем через ZSL_122 (только валидные IMEI длиной >= 6)
    missing_imeis = [m for m in (target_imeis - set(found_devices.keys()) - {e['deviceId'] for e in found_errors}) if len(m) >= 6]
    for m_imei in missing_imeis[:3]:  # Ограничиваем до 3 параллельно, чтобы не зависать
        existing_info = await loop.run_in_executor(None, query_single_device_info, m_imei, 3)
        if existing_info:
            found_devices[m_imei] = existing_info

    # 5. Сохраняем сырой ответ
    if raw_response_xml:
        with open(response_xml_path, "w", encoding="utf-8") as f:
            f.write(raw_response_xml)

    # 6. Формируем отчеты TXT и CSV
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # CSV
    with open(result_csv_path, "w", encoding="utf-8") as f:
        f.write("IMEI;GeoLocatorNumber;GeoLocatorPIN;Status;CreationDate;AdditionalInformation;Type\n")
        for imei, d in found_devices.items():
            reg_type = "EXISTING" if d.get("is_existing") else "NEW"
            f.write(f"{d['deviceId']};{d['locNum']};{d['locPin']};{d['status']};{d['created']};{d['info']};{reg_type}\n")
        for e in found_errors:
            f.write(f"{e['deviceId']};;;;;ERROR: {e['reason']}\n")

    # TXT
    with open(result_txt_path, "w", encoding="utf-8") as f:
        f.write("=" * 85 + "\n")
        f.write("ДОКУМЕНТ: РЕЗУЛЬТАТЫ РЕГИСТРАЦИИ УСТРОЙСТВ В PUESC (e-TOLL / SENT)\n")
        f.write(f"Контрагент: {contractor or 'Общий'}\n")
        f.write(f"Оператор: {OBE_SERVICE_NUMBER} ({OBE_OPERATOR_IDENTITY_NUMBER})\n")
        f.write(f"Дата формирования: {now_str}\n")
        f.write("=" * 85 + "\n")
        f.write(f"{'IMEI':<18} | {'Номер локатора (ID)':<22} | {'PIN-код':<8} | {'Статус':<8} | {'Дата / Примечание'}\n")
        f.write("-" * 85 + "\n")
        for imei, d in found_devices.items():
            note = "(ранее в базе)" if d.get("is_existing") else "(новый)"
            f.write(f"{d['deviceId']:<18} | {d['locNum']:<22} | {d['locPin']:<8} | {d['status']:<8} | {d['created']} {note}\n")
        for e in found_errors:
            f.write(f"{e['deviceId']:<18} | {'-':<22} | {'-':<8} | {'ОШИБКА':<8} | {e['reason']}\n")
        f.write("=" * 85 + "\n")

    # Преобразуем в структурированный список результатов
    results_list = []
    req_map = {str(d["imei"]).strip(): d for d in devices_data}

    for imei in target_imeis:
        src = req_map.get(imei, {})
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

    # 3. Сохраняем отчеты
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(result_csv_path, "w", encoding="utf-8") as f:
        f.write("IMEI;GeoLocatorNumber;Status;ModificationDate;Result\n")
        for imei, d in removed_now.items():
            f.write(f"{d['deviceId']};{d['locNum']};4;{d['modDate']};REMOVED_NOW\n")
        for imei in target_set - set(removed_now.keys()):
            f.write(f"{imei};;;;ALREADY_REMOVED_OR_NOT_FOUND\n")

    with open(result_txt_path, "w", encoding="utf-8") as f:
        f.write("=" * 85 + "\n")
        f.write("ДОКУМЕНТ: РЕЗУЛЬТАТЫ УДАЛЕНИЯ УСТРОЙСТВ ИЗ PUESC (e-TOLL)\n")
        f.write(f"Оператор: {OBE_SERVICE_NUMBER} ({OBE_OPERATOR_IDENTITY_NUMBER})\n")
        f.write(f"Дата формирования: {now_str}\n")
        f.write("=" * 85 + "\n")
        for imei in target_set:
            if imei in removed_now:
                f.write(f"IMEI: {imei:<18} | Номер: {removed_now[imei]['locNum']:<22} | СТАТУС: УДАЛЕНО\n")
            else:
                f.write(f"IMEI: {imei:<18} | СТАТУС: УЖЕ ДЕАКТИВИРОВАНО / НЕ НАЙДЕНО\n")
        f.write("=" * 85 + "\n")

    results_list = []
    for imei in target_set:
        if imei in removed_now:
            results_list.append({
                "imei": imei,
                "status": "4 (Удалено)",
                "result": "Успешно удалено",
                "locNum": removed_now[imei]["locNum"],
                "success": True,
            })
        else:
            results_list.append({
                "imei": imei,
                "status": "4 (Деактивировано)",
                "result": "Уже было удалено ранее",
                "locNum": "-",
                "success": True,
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
    app.router.add_get("/api/download/{filename}", handle_api_download)
    app.router.add_get("/api/history", handle_api_history)
    app.router.add_static("/static/", path=str(STATIC_DIR), name="static")
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print("=" * 70)
    print(f"🚀 PUESC e-TOLL / SENT Web Service запущен на http://localhost:{port}")
    print(f"📁 Папка отчетов: {REPORTS_DIR}")
    print("=" * 70)
    web.run_app(init_app(), host="0.0.0.0", port=port)
