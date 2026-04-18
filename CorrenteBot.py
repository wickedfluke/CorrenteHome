#!/usr/bin/env python3
"""Telegram bot per monitorare connettivita', batteria e alimentazione su Termux.

Comandi:
- /start: registra la chat per ricevere gli avvisi
- /modem: verifica se la connessione internet funziona

Il bot non si ferma se internet cade: continua a monitorare localmente e invia
gli avvisi appena la connessione torna disponibile.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "CorrenteBot.log"
STATE_FILE = BASE_DIR / "CorrenteBot.state.json"

BOT_TOKEN = "8768567297:AAFi2g7iKdDJKW349hO8PirzRZkMT7fb4Hw"
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
UPDATE_TIMEOUT_SECONDS = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "20"))
MONITOR_INTERVAL_SECONDS = int(os.environ.get("MONITOR_INTERVAL_SECONDS", "30"))
INTERNET_CHECK_URL = os.environ.get("INTERNET_CHECK_URL", "https://www.google.com/generate_204")
INTERNET_CHECK_TIMEOUT = int(os.environ.get("INTERNET_CHECK_TIMEOUT", "5"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG").upper()


logging.basicConfig(
	level=getattr(logging, LOG_LEVEL, logging.DEBUG),
	format="%(asctime)s %(levelname)s %(message)s",
	handlers=[
		logging.FileHandler(LOG_FILE, encoding="utf-8"),
		logging.StreamHandler(),
	],
)
logger = logging.getLogger("CorrenteBot")


def now_iso() -> str:
	return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
	if not value:
		return None
	return datetime.fromisoformat(value)


def fmt_duration(seconds: float) -> str:
	total = max(0, int(seconds))
	hours, remainder = divmod(total, 3600)
	minutes, seconds_left = divmod(remainder, 60)
	parts: list[str] = []
	if hours:
		parts.append(f"{hours}h")
	if minutes or hours:
		parts.append(f"{minutes}m")
	parts.append(f"{seconds_left}s")
	return " ".join(parts)


def safe_int(value: Any, default: int | None = None) -> int | None:
	try:
		if value is None:
			return default
		return int(value)
	except (TypeError, ValueError):
		return default


def http_get(url: str, timeout: int) -> tuple[bool, int | None, str]:
	logger.debug("HTTP GET start url=%s timeout=%s", url, timeout)
	request = urllib.request.Request(url, headers={"User-Agent": "CorrenteBot/1.0"})
	try:
		with urllib.request.urlopen(request, timeout=timeout) as response:
			body = response.read(256).decode("utf-8", errors="replace")
			logger.debug("HTTP GET ok url=%s status=%s body_preview=%r", url, response.status, body[:120])
			return True, response.status, body
	except Exception as exc:  # broad on purpose: network failures are expected
		logger.debug("HTTP GET failed url=%s error=%s", url, exc)
		return False, None, str(exc)


def telegram_api(method: str, params: dict[str, Any] | None = None, timeout: int = 20) -> Any:
	logger.debug("Telegram API call start method=%s timeout=%s params=%s", method, timeout, sorted((params or {}).keys()))
	data = None
	url = f"{API_BASE}/{method}"
	headers = {"User-Agent": "CorrenteBot/1.0"}
	if params:
		encoded = urllib.parse.urlencode(params, doseq=True).encode("utf-8")
		data = encoded
		headers["Content-Type"] = "application/x-www-form-urlencoded"

	request = urllib.request.Request(url, data=data, headers=headers)
	try:
		with urllib.request.urlopen(request, timeout=timeout) as response:
			payload = response.read().decode("utf-8")
			logger.debug("Telegram API response method=%s payload_preview=%r", method, payload[:240])
	except Exception as exc:
		logger.exception("Telegram API transport failure method=%s error=%s", method, exc)
		raise
	result = json.loads(payload)
	if not result.get("ok"):
		logger.error("Telegram API not ok method=%s result=%s", method, result)
		raise RuntimeError(f"Telegram API error: {result}")
	logger.debug("Telegram API call ok method=%s", method)
	return result["result"]


def send_message(chat_id: int, text: str) -> None:
	logger.debug("send_message start chat_id=%s text_preview=%r", chat_id, text[:240])
	telegram_api(
		"sendMessage",
		{
			"chat_id": chat_id,
			"text": text,
			"disable_web_page_preview": True,
		},
		timeout=20,
	)
	logger.debug("send_message ok chat_id=%s", chat_id)


def send_to_known_chats(state: "BotState", text: str) -> None:
	logger.info("Broadcast to %s chats: %s", len(state.chat_ids), text.splitlines()[0] if text else "<empty>")
	for chat_id in sorted(state.chat_ids):
		try:
			logger.debug("Broadcasting to chat_id=%s", chat_id)
			send_message(chat_id, text)
		except Exception as exc:
			logger.warning("Invio fallito a chat %s: %s", chat_id, exc)
			state.pending_messages.append({"chat_id": chat_id, "text": text, "created_at": now_iso()})


def flush_pending_messages(state: BotState) -> None:
	if not state.pending_messages:
		return
	pending = state.pending_messages[:]
	state.pending_messages.clear()
	for item in pending:
		try:
			send_message(int(item["chat_id"]), str(item["text"]))
		except Exception as exc:
			logger.warning("Rinvio messaggio fallito verso %s: %s", item.get("chat_id"), exc)
			state.pending_messages.append(item)
			break


def detect_internet() -> tuple[bool, str]:
	ok, status, detail = http_get(INTERNET_CHECK_URL, INTERNET_CHECK_TIMEOUT)
	if ok:
		return True, f"HTTP {status}"
	return False, detail


def read_termux_battery_status() -> dict[str, Any] | None:
	try:
		output = subprocess.check_output(["termux-battery-status"], stderr=subprocess.STDOUT, text=True)
		return json.loads(output)
	except Exception:
		return None


def read_sysfs_value(path: Path) -> str | None:
	try:
		return path.read_text(encoding="utf-8").strip()
	except Exception:
		return None


def detect_power_and_battery() -> dict[str, Any]:
	termux_status = read_termux_battery_status()
	if termux_status:
		status = str(termux_status.get("status", "unknown")).lower()
		percent = safe_int(termux_status.get("percentage"))
		charging = status in {"charging", "full"}
		return {
			"percentage": percent,
			"charging": charging,
			"status": status,
			"source": "termux-api",
		}

	battery_dir = Path("/sys/class/power_supply")
	candidates = [battery_dir / "battery", battery_dir / "BAT0"]
	battery_path = next((path for path in candidates if path.exists()), None)
	if battery_path is None:
		return {"percentage": None, "charging": None, "status": "unknown", "source": "unavailable"}

	percent = safe_int(read_sysfs_value(battery_path / "capacity"))
	status = (read_sysfs_value(battery_path / "status") or "unknown").lower()
	charging = status in {"charging", "full"}

	ac_online = read_sysfs_value(battery_dir / "ac" / "online")
	usb_online = read_sysfs_value(battery_dir / "usb" / "online")
	mains_online = read_sysfs_value(battery_dir / "mains" / "online")
	if any(value == "1" for value in (ac_online, usb_online, mains_online) if value is not None):
		charging = True

	return {
		"percentage": percent,
		"charging": charging,
		"status": status,
		"source": "sysfs",
	}


@dataclass
class BotState:
	chat_ids: set[int] = field(default_factory=set)
	pending_messages: list[dict[str, Any]] = field(default_factory=list)
	last_update_id: int | None = None
	internet_up: bool | None = None
	internet_down_since: str | None = None
	internet_down_battery: int | None = None
	internet_down_charging: bool | None = None
	internet_down_power_state: str | None = None
	last_battery_percentage: int | None = None
	last_charging: bool | None = None
	last_power_state: str | None = None
	last_status_snapshot: dict[str, Any] = field(default_factory=dict)


def load_state() -> BotState:
	logger.debug("Loading state from %s", STATE_FILE)
	if not STATE_FILE.exists():
		logger.debug("State file missing, starting fresh")
		return BotState()
	try:
		data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
	except Exception as exc:
		logger.warning("Impossibile leggere lo stato salvato: %s", exc)
		return BotState()

	state = BotState()
	state.chat_ids = set(int(chat_id) for chat_id in data.get("chat_ids", []))
	state.pending_messages = list(data.get("pending_messages", []))
	state.last_update_id = data.get("last_update_id")
	state.internet_up = data.get("internet_up")
	state.internet_down_since = data.get("internet_down_since")
	state.internet_down_battery = data.get("internet_down_battery")
	state.internet_down_charging = data.get("internet_down_charging")
	state.internet_down_power_state = data.get("internet_down_power_state")
	state.last_battery_percentage = data.get("last_battery_percentage")
	state.last_charging = data.get("last_charging")
	state.last_power_state = data.get("last_power_state")
	state.last_status_snapshot = dict(data.get("last_status_snapshot", {}))
	logger.info("State loaded chats=%s pending=%s last_update_id=%s internet_up=%s", len(state.chat_ids), len(state.pending_messages), state.last_update_id, state.internet_up)
	return state


def save_state(state: BotState) -> None:
	logger.debug("Saving state chats=%s pending=%s last_update_id=%s internet_up=%s", len(state.chat_ids), len(state.pending_messages), state.last_update_id, state.internet_up)
	payload = {
		"chat_ids": sorted(state.chat_ids),
		"pending_messages": state.pending_messages,
		"last_update_id": state.last_update_id,
		"internet_up": state.internet_up,
		"internet_down_since": state.internet_down_since,
		"internet_down_battery": state.internet_down_battery,
		"internet_down_charging": state.internet_down_charging,
		"internet_down_power_state": state.internet_down_power_state,
		"last_battery_percentage": state.last_battery_percentage,
		"last_charging": state.last_charging,
		"last_power_state": state.last_power_state,
		"last_status_snapshot": state.last_status_snapshot,
	}
	STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
	logger.debug("State saved to %s", STATE_FILE)


def snapshot_status() -> dict[str, Any]:
	logger.debug("Creating status snapshot")
	internet_ok, internet_detail = detect_internet()
	power = detect_power_and_battery()
	snapshot = {
		"timestamp": now_iso(),
		"internet_ok": internet_ok,
		"internet_detail": internet_detail,
		**power,
	}
	logger.info(
		"Snapshot internet_ok=%s detail=%s battery=%s charging=%s power_state=%s source=%s",
		snapshot.get("internet_ok"),
		snapshot.get("internet_detail"),
		snapshot.get("percentage"),
		snapshot.get("charging"),
		snapshot.get("status"),
		snapshot.get("source"),
	)
	return snapshot


def update_state_from_snapshot(state: BotState, snapshot: dict[str, Any]) -> None:
	logger.debug("Updating state from snapshot timestamp=%s", snapshot.get("timestamp"))
	state.last_status_snapshot = snapshot
	state.last_battery_percentage = snapshot.get("percentage")
	state.last_charging = snapshot.get("charging")
	state.last_power_state = snapshot.get("status")


def handle_modem_command(state: BotState, chat_id: int) -> None:
	logger.info("/modem requested chat_id=%s", chat_id)
	try:
		snapshot = snapshot_status()
		update_state_from_snapshot(state, snapshot)
		if snapshot["internet_ok"]:
			text = "Modem funzionante"
		else:
			battery = snapshot.get("percentage")
			charging = "in carica" if snapshot.get("charging") else "non in carica"
			text = (
				"Modem non funzionante\n"
				f"Dettaglio rete: {snapshot.get('internet_detail')}\n"
				f"Batteria: {battery if battery is not None else 'n/d'}%\n"
				f"Stato alimentazione: {charging}"
			)
	except Exception as exc:
		logger.exception("Errore durante /modem: %s", exc)
		text = f"Impossibile verificare il modem: {exc}"

	try:
		logger.debug("Sending /modem response chat_id=%s text=%r", chat_id, text)
		send_message(chat_id, text)
	except Exception as exc:
		logger.warning("Impossibile inviare la risposta a /modem: %s", exc)
	save_state(state)


def handle_start_command(state: BotState, chat_id: int) -> None:
	logger.info("/start requested chat_id=%s", chat_id)
	state.chat_ids.add(chat_id)
	try:
		send_message(chat_id, "Chat registrata. Usero' questo chat id per gli avvisi e il comando /modem.")
	except Exception as exc:
		logger.warning("Impossibile inviare conferma /start a chat_id=%s: %s", chat_id, exc)
	save_state(state)


def process_update(state: BotState, update: dict[str, Any]) -> None:
	logger.debug("Processing update raw=%s", update)
	update_id = update.get("update_id")
	if isinstance(update_id, int):
		state.last_update_id = update_id
		logger.debug("Updated last_update_id=%s", state.last_update_id)

	message = update.get("message") or update.get("edited_message")
	if not isinstance(message, dict):
		return

	chat = message.get("chat") or {}
	chat_id = chat.get("id")
	if not isinstance(chat_id, int):
		return

	logger.info("Incoming message chat_id=%s text=%r", chat_id, (message.get("text") or ""))
	state.chat_ids.add(chat_id)
	text = (message.get("text") or "").strip()
	if text.startswith("/start"):
		logger.debug("Dispatching /start for chat_id=%s", chat_id)
		handle_start_command(state, chat_id)
	elif text.startswith("/modem"):
		logger.debug("Dispatching /modem for chat_id=%s", chat_id)
		handle_modem_command(state, chat_id)
	else:
		logger.debug("No command matched for chat_id=%s", chat_id)
		save_state(state)


def telegram_poll_loop(state: BotState) -> None:
	logger.info("Telegram poll loop started timeout=%s", UPDATE_TIMEOUT_SECONDS)
	while True:
		try:
			params: dict[str, Any] = {"timeout": UPDATE_TIMEOUT_SECONDS}
			if state.last_update_id is not None:
				params["offset"] = state.last_update_id + 1
			logger.debug("Polling Telegram with params=%s", params)
			updates = telegram_api("getUpdates", params, timeout=UPDATE_TIMEOUT_SECONDS + 10)
			logger.info("Telegram updates received count=%s", len(updates))
			for update in updates:
				try:
					process_update(state, update)
				except Exception as exc:
					logger.exception("Errore mentre processavo un update: %s", exc)
				finally:
					save_state(state)
		except Exception as exc:
			logger.warning("Polling Telegram non disponibile: %s", exc)
			time.sleep(5)


def build_outage_message(state: BotState, snapshot: dict[str, Any], event: str) -> str:
	logger.debug("Building outage message event=%s snapshot=%s", event, snapshot)
	battery = snapshot.get("percentage")
	charging = "in carica" if snapshot.get("charging") else "non in carica"
	return (
		f"{event}\n"
		f"Ora: {snapshot.get('timestamp')}\n"
		f"Rete: {snapshot.get('internet_detail')}\n"
		f"Batteria: {battery if battery is not None else 'n/d'}%\n"
		f"Alimentazione: {charging}"
	)


def monitor_loop(state: BotState) -> None:
	logger.info("Monitor loop started interval=%s", MONITOR_INTERVAL_SECONDS)
	while True:
		try:
			logger.debug("Monitor cycle start internet_up=%s last_down_since=%s", state.internet_up, state.internet_down_since)
			snapshot = snapshot_status()
			update_state_from_snapshot(state, snapshot)

			internet_ok = bool(snapshot.get("internet_ok"))
			current_battery = snapshot.get("percentage")
			current_charging = snapshot.get("charging")
			current_power_state = snapshot.get("status")

			if state.internet_up is None:
				state.internet_up = internet_ok
				logger.debug("Initializing internet state to %s", internet_ok)
				if not internet_ok:
					logger.info("Internet initially down at %s", snapshot["timestamp"])
					state.internet_down_since = snapshot["timestamp"]
					state.internet_down_battery = current_battery
					state.internet_down_charging = current_charging
					state.internet_down_power_state = current_power_state

			elif state.internet_up and not internet_ok:
				logger.info("Internet transition up -> down at %s", snapshot["timestamp"])
				state.internet_up = False
				state.internet_down_since = snapshot["timestamp"]
				state.internet_down_battery = current_battery
				state.internet_down_charging = current_charging
				state.internet_down_power_state = current_power_state
				message = build_outage_message(state, snapshot, "Connessione internet persa")
				logger.warning(message.replace("\n", " | "))
				send_to_known_chats(state, message)

			elif not state.internet_up and internet_ok:
				logger.info("Internet transition down -> up at %s", snapshot["timestamp"])
				state.internet_up = True
				recovered_at = datetime.fromisoformat(snapshot["timestamp"])
				started_at = parse_iso(state.internet_down_since) or recovered_at
				outage_duration = fmt_duration((recovered_at - started_at).total_seconds())
				start_battery = state.internet_down_battery
				battery_delta = None
				if start_battery is not None and current_battery is not None:
					battery_delta = start_battery - current_battery

				message_lines = [
					"Connessione internet tornata",
					f"Fuori rete per: {outage_duration}",
					f"Rete: {snapshot.get('internet_detail')}",
					f"Batteria iniziale: {start_battery if start_battery is not None else 'n/d'}%",
					f"Batteria attuale: {current_battery if current_battery is not None else 'n/d'}%",
					f"Scarica durante il blackout: {battery_delta if battery_delta is not None else 'n/d'}%",
					f"Alimentazione iniziale: {'in carica' if state.internet_down_charging else 'non in carica' if state.internet_down_charging is not None else 'n/d'}",
					f"Alimentazione attuale: {'in carica' if current_charging else 'non in carica' if current_charging is not None else 'n/d'}",
				]
				message = "\n".join(message_lines)
				logger.info(message.replace("\n", " | "))
				send_to_known_chats(state, message)
				state.internet_down_since = None
				state.internet_down_battery = None
				state.internet_down_charging = None
				state.internet_down_power_state = None

			elif not internet_ok and state.internet_down_since:
				logger.debug("Internet still down; checking battery and power deltas")
				previous_battery = state.internet_down_battery
				previous_charging = state.internet_down_charging
				battery_changed = previous_battery is not None and current_battery is not None and current_battery < previous_battery
				charging_changed = previous_charging is not None and current_charging is not None and current_charging != previous_charging
				if battery_changed or charging_changed:
					logger.info("Blackout update battery_changed=%s charging_changed=%s", battery_changed, charging_changed)
					state.internet_down_battery = current_battery if current_battery is not None else previous_battery
					state.internet_down_charging = current_charging if current_charging is not None else previous_charging
					detail_parts = []
					if battery_changed:
						detail_parts.append(f"batteria scesa da {previous_battery}% a {current_battery}%")
					if charging_changed:
						detail_parts.append(
							f"alimentazione cambiata da {'in carica' if previous_charging else 'non in carica'} a {'in carica' if current_charging else 'non in carica'}"
						)
					message = "Aggiornamento blackout: " + "; ".join(detail_parts)
					logger.info(message)
					send_to_known_chats(state, message)

			state.last_status_snapshot = snapshot
			save_state(state)
		except Exception as exc:
			logger.exception("Errore nel monitoraggio: %s", exc)

		logger.debug("Monitor cycle sleeping for %s seconds", MONITOR_INTERVAL_SECONDS)
		time.sleep(MONITOR_INTERVAL_SECONDS)


def main() -> None:
	logger.info("CorrenteBot starting")
	state = load_state()
	logger.info("Bot avviato. Chat registrate: %s", sorted(state.chat_ids))

	monitor_thread = threading.Thread(target=monitor_loop, args=(state,), daemon=True)
	logger.info("Starting monitor thread")
	monitor_thread.start()

	logger.info("Entering Telegram poll loop")
	telegram_poll_loop(state)


if __name__ == "__main__":
	main()
