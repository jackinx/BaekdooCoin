#!/usr/bin/env python3
"""업비트 API IP 변경 감지 — LLM 불필요, 크론 command 직실행
IP 바뀌면 알림 파일 기록 (Watchdog가 릴레이)"""
import os, sys, json, time, requests, jwt, uuid
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = Path("/tmp/baekdoocoin_ip_state.json")
ALERT_FILE = Path("/tmp/baekdoocoin_ip_alert.jsonl")

try:
    import dotenv
    env_path = PROJECT_DIR / ".env"
    if env_path.exists():
        dotenv.load_dotenv(env_path)
except ImportError:
    pass

ACCESS_KEY = os.getenv("UPBIT_ACCESS_KEY", "")
SECRET_KEY = os.getenv("UPBIT_SECRET_KEY", "")


def get_external_ip():
    for url, timeout in [("https://api.ipify.org", 5), ("https://checkip.amazonaws.com", 5)]:
        try:
            ip = requests.get(url, timeout=timeout).text.strip()
            if ip:
                return ip
        except Exception:
            continue
    return None


def test_upbit():
    if not ACCESS_KEY or not SECRET_KEY:
        return False, "keys missing"
    payload = {"access_key": ACCESS_KEY, "nonce": str(uuid.uuid4())}
    token = jwt.encode(payload, key=SECRET_KEY, algorithm="HS256")
    try:
        res = requests.get("https://api.upbit.com/v1/accounts",
                           headers={"Authorization": f"Bearer {token}"}, timeout=10)
        return (res.status_code == 200, "" if res.status_code == 200 else res.text[:200])
    except Exception as e:
        return (False, str(e))


def main():
    current_ip = get_external_ip()
    if not current_ip:
        print("ERR: no IP"); sys.exit(1)

    prev = {}
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text())
        except Exception:
            pass

    prev_ip = prev.get("last_ip", "")
    api_ok, err = test_upbit()

    prev["last_ip"] = current_ip
    prev["last_check_epoch"] = int(time.time())
    STATE_FILE.write_text(json.dumps(prev))

    status = "OK" if api_ok else f"BLOCKED:{err[:80]}"
    print(f"{status} ip={current_ip}")

    if not prev_ip:
        return  # first run

    if current_ip == prev_ip:
        return  # no change

    # IP changed — write alert for relay
    alert = {"ts": time.time(), "type": "ip_change",
             "old": prev_ip, "new": current_ip, "api_ok": api_ok}
    if not api_ok:
        alert["error"] = err[:300]

    with open(ALERT_FILE, "a") as f:
        f.write(json.dumps(alert, ensure_ascii=False) + "\n")

    print(f"🚨 ALERT: {prev_ip} → {current_ip} ({'OK' if api_ok else 'BLOCKED'})")


if __name__ == "__main__":
    main()