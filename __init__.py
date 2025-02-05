"""
OmniPrompt Anki Add‑on

Features:
- A Tools menu entry “OmniPrompt” (as a submenu inside the Tools menu) with “Settings” and “About” items.
- A browser context‑menu action “Update with OmniPrompt” (on right‑click on a note).
- A Settings dialog to set AI provider (API key, temperature, max tokens), view the log, and access Advanced Settings.
- An Update dialog that opens when “Update with OmniPrompt” is triggered. 
  - Left panel: prompt editing / saved-prompt selection (using triple‑brackets delimiters), 
    output field dropdown (populated from the first selected note), Start/Stop buttons.
  - Right panel: table with columns: “Progress”, “Original”, “Generated.”
- Automatically writes generated text into the chosen output field.
"""

import requests, logging, os, time, socket, sys, json
from jsonschema import validate
from anki.errors import NotFoundError
from aqt.utils import showInfo, getText
from PyQt6.QtCore import QTimer, Qt, QThread, pyqtSignal
from PyQt6.QtGui import (
    QAction, 
    QDoubleValidator, 
    QIntValidator, 
    QKeySequence, 
    QShortcut
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QGroupBox, QComboBox, QLabel,
    QLineEdit, QFormLayout, QPushButton, QTextEdit, QHBoxLayout,
    QWidget, QTableWidget, QTableWidgetItem, QMenu
)
from aqt import mw, gui_hooks
from aqt.browser import Browser
from anki.hooks import addHook
from logging.handlers import RotatingFileHandler

# -------------------------------
# Constants & Default Config
# -------------------------------
AI_PROVIDERS = ["openai", "deepseek"]

DEFAULT_CONFIG = {
    "_version": 1.1,
    "AI_PROVIDER": "openai",
    "OPENAI_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    "API_ENDPOINT": "api.openai.com",
    "OPENAI_MODEL": "gpt-4o-mini",
    "DEEPSEEK_MODEL": "deepseek-chat",
    "TEMPERATURE": 0.2,
    "MAX_TOKENS": 200,
    "API_DELAY": 1,       # Delay (in seconds) between API calls
    "TIMEOUT": 20,        # API request timeout in seconds
    "PROMPT": "Paste your prompt here.",
    "SELECTED_FIELDS": {
        "output_field": "Output"
    },
    # For demonstration, let's let the user enable/disable streaming in DeepSeek:
    "DEEPSEEK_STREAM": False
}

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "_version": {"type": "number"},
        "AI_PROVIDER": {"enum": AI_PROVIDERS},
        "OPENAI_API_KEY": {"type": "string"},
        "DEEPSEEK_API_KEY": {"type": "string"},
        "OPENAI_MODEL": {"type": "string"},
        "DEEPSEEK_MODEL": {"type": "string"},
        "TEMPERATURE": {"type": "number"},
        "MAX_TOKENS": {"type": "integer"},
        "API_DELAY": {"type": "number"},
        "TIMEOUT": {"type": "number"},
        "PROMPT": {"type": "string"},
        "DEEPSEEK_STREAM": {"type": "boolean"},
        "SELECTED_FIELDS": {
            "type": "object",
            "properties": {
                "output_field": {"type": "string"}
            }
        }
    },
    "required": ["AI_PROVIDER"]
}

# -------------------------------
# Helper Functions
# -------------------------------
def safe_show_info(message: str) -> None:
    """Use QTimer to safely show a modal message from within a threaded context."""
    QTimer.singleShot(0, lambda: showInfo(message))

def load_prompt_templates() -> dict:
    """Loads prompts from prompt_templates.txt using [[[Name]]] delimiters."""
    templates_path = os.path.join(os.path.dirname(__file__), "prompt_templates.txt")
    templates = {}
    if os.path.exists(templates_path):
        with open(templates_path, "r", encoding="utf-8") as file:
            current_key = None
            current_value = []
            for line in file:
                line = line.rstrip('\n')
                if line.startswith("[[[") and line.endswith("]]]"):
                    # Save the previous template if any
                    if current_key is not None:
                        templates[current_key] = "\n".join(current_value)
                    current_key = line[3:-3].strip()
                    current_value = []
                else:
                    current_value.append(line)
            # Save the last template if in progress
            if current_key is not None:
                templates[current_key] = "\n".join(current_value)
    return templates

def save_prompt_templates(templates: dict) -> None:
    """Writes updated prompt templates to disk."""
    templates_path = os.path.join(os.path.dirname(__file__), "prompt_templates.txt")
    os.makedirs(os.path.dirname(templates_path), exist_ok=True)
    with open(templates_path, "w", encoding="utf-8", newline="\n") as file:
        for key, value in sorted(templates.items()):
            file.write(f"[[[{key}]]]\n{value}\n\n")

def check_internet() -> bool:
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=5)
        return True
    except OSError:
        return False

# -------------------------------
# Logger Setup
# -------------------------------
def get_addon_dir() -> str:
    raw_dir = os.path.dirname(__file__)
    parent = os.path.dirname(raw_dir)
    base = os.path.basename(raw_dir).strip()
    return os.path.join(parent, base)

def setup_logger() -> logging.Logger:
    logger_obj = logging.getLogger("OmniPromptAnki")
    logger_obj.setLevel(logging.INFO)
    addon_dir = get_addon_dir()
    log_file = os.path.join(addon_dir, "omnPrompt-anki.log")
    handler = SafeAnkiRotatingFileHandler(
        filename=log_file,
        mode='a',
        maxBytes=5 * 1024 * 1024,
        backupCount=2,
        encoding='utf-8',
        delay=True
    )
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    if not logger_obj.handlers:
        logger_obj.addHandler(handler)
    return logger_obj

class SafeAnkiRotatingFileHandler(RotatingFileHandler):
    """A rotating file handler that logs exceptions on rollover failures."""
    def emit(self, record):
        try:
            super().emit(record)
        except Exception as e:
            print(f"Log write failed: {str(e)}")

    def shouldRollover(self, record) -> bool:
        try:
            return super().shouldRollover(record)
        except Exception as e:
            print(f"Log rotation check failed: {str(e)}")
            return False

    def doRollover(self):
        try:
            super().doRollover()
            print("Successfully rotated log file")
        except PermissionError:
            print("Couldn't rotate log - file in use")
        except Exception as e:
            print(f"Log rotation failed: {str(e)}")

def check_log_size():
    """Check if the log is near capacity on each reset event."""
    log_path = os.path.join(mw.addonManager.addonsFolder(), "omniprompt-anki", "omnPrompt-anki.log")
    try:
        size = os.path.getsize(log_path)
        if size > 4.5 * 1024 * 1024:
            print("Log file approaching maximum size")
    except Exception:
        pass

addHook("reset", check_log_size)
logger = setup_logger()

# -------------------------------
# Background Worker for Note Processing
# -------------------------------
class NoteProcessingWorker(QThread):
    # We now emit partial progress with a (rowIndex, percentage) signature
    progress_update = pyqtSignal(int, int)
    note_result = pyqtSignal(object, str)     # (note, explanation)
    error_occurred = pyqtSignal(object, str)  # (note, error_string)
    finished_processing = pyqtSignal(int, int, int)  # (processed, total, error_count)

    def __init__(self, note_prompts: list, generate_ai_response_callback, parent=None):
        super().__init__(parent)
        self.note_prompts = note_prompts
        self.generate_ai_response_callback = generate_ai_response_callback
        self._is_cancelled = False
        self.processed = 0
        self.error_count = 0

    def run(self) -> None:
        total = len(self.note_prompts)
        for i, (note, prompt) in enumerate(self.note_prompts):
            if self._is_cancelled:
                break

            # We'll emit "progress" = 0% at the start of each note
            self.progress_update.emit(i, 0)

            try:
                # pass a closure that updates partial progress for this note
                def per_chunk_progress(pct):
                    # clamp to [0..99] unless final
                    if pct >= 100: 
                        pct = 99
                    self.progress_update.emit(i, pct)

                explanation = self.generate_ai_response_callback(prompt, stream_progress_callback=per_chunk_progress)
                # Once we have the final text, set progress to 100%
                self.progress_update.emit(i, 100)
                self.note_result.emit(note, explanation)
            except Exception as e:
                self.error_count += 1
                logger.exception(f"Error processing note {note.id}")
                self.error_occurred.emit(note, str(e))

            self.processed += 1

        self.finished_processing.emit(self.processed, total, self.error_count)

    def cancel(self) -> None:
        self._is_cancelled = True

# -------------------------------
# OmniPromptManager Class
#  (renamed from GPTGrammarExplainer)
# -------------------------------
class OmniPromptManager:
    @property
    def addon_dir(self) -> str:
        return os.path.dirname(__file__)

    def __init__(self):
        self.config = self.load_config()
        mw.addonManager.setConfigAction(__name__, self.show_settings_dialog)

    def load_config(self) -> dict:
        raw_config = mw.addonManager.getConfig(__name__) or {}
        validated = self.validate_config(raw_config)
        return self.migrate_config(validated)

    def migrate_config(self, config: dict) -> dict:
        """More graceful migrations. Only reset if we cannot handle older data."""
        current_version = config.get("_version", 0)
        if current_version < 1.0:
            # Suppose we can't handle anything older than 1.0
            logger.info(f"Config too old to migrate (version {current_version}). Forcing reset.")
            return DEFAULT_CONFIG.copy()

        # Example: If we had future migrations, we'd do them step by step:
        # if current_version < 1.1:
        #     config.setdefault("SOME_NEW_FIELD", "default_value")
        #     config["_version"] = 1.1

        # Now ensure required fields from defaults:
        migrated = DEFAULT_CONFIG.copy()
        migrated.update(config)

        # If the final version is still below 1.1, bump it.
        if migrated["_version"] < 1.1:
            migrated.setdefault("SELECTED_FIELDS", DEFAULT_CONFIG["SELECTED_FIELDS"])
            migrated["_version"] = 1.1

        return migrated

    def validate_config(self, config: dict) -> dict:
        try:
            validate(instance=config, schema=CONFIG_SCHEMA)
            return config
        except Exception as e:
            logger.exception(f"Config validation error: {str(e)}")
            logger.info("Reverting to default configuration")
            return DEFAULT_CONFIG.copy()

    def show_settings_dialog(self) -> None:
        dialog = SettingsDialog(mw)
        dialog.load_config(self.config)
        if dialog.exec():
            self.config = dialog.get_updated_config()
            self.save_config()

    def save_config(self) -> None:
        """Write updated config back to Anki, forcibly reset only if migration truly fails."""
        try:
            validated = self.validate_config(self.config)
            migrated = self.migrate_config(validated)
            mw.addonManager.writeConfig(__name__, migrated)
        except Exception as e:
            logger.exception(f"Config save failed: {str(e)}")
            self.restore_config()

    def backup_config(self) -> None:
        backup_path = os.path.join(self.addon_dir, "config_backup.json")
        try:
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f)
        except Exception as e:
            logger.exception(f"Failed to backup config: {e}")

    def restore_config(self) -> None:
        backup_path = os.path.join(self.addon_dir, "config_backup.json")
        if os.path.exists(backup_path):
            try:
                with open(backup_path, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
                self.save_config()
            except Exception as e:
                logger.exception(f"Failed to restore config from backup: {e}")

    # -------------------------------
    # AI Request Methods
    # -------------------------------
    def generate_ai_response(self, prompt: str, stream_progress_callback=None) -> str:
        """Entry point to generate text from the selected AI provider."""
        provider = self.config.get("AI_PROVIDER", "openai")
        if provider == "openai":
            return self.make_openai_request(prompt)
        elif provider == "deepseek":
            if not self.config.get("DEEPSEEK_MODEL"):
                return "[Error: No DeepSeek model selected]"
            return self.make_deepseek_request(prompt, stream_progress_callback=stream_progress_callback)
        else:
            logger.error(f"Invalid AI provider: {provider}")
            return "[Error: Invalid AI provider]"

    def make_openai_request(self, prompt: str) -> str:
        """Non-streaming example with retries/backoff in send_request()."""
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config['OPENAI_API_KEY']}"
        }
        data = {
            "model": self.config["OPENAI_MODEL"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.config["MAX_TOKENS"],
            "temperature": self.config["TEMPERATURE"]
        }
        return self.send_request(url, headers, data)

    def make_deepseek_request(self, prompt: str, stream_progress_callback=None) -> str:
        """Handles optional streaming from DeepSeek."""
        url = "https://api.deepseek.com/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config['DEEPSEEK_API_KEY']}"
        }
        stream_flag = self.config.get("DEEPSEEK_STREAM", False)
        data = {
            "model": self.config.get("DEEPSEEK_MODEL", "deepseek-chat"),
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            "temperature": self.config.get("TEMPERATURE", 0.2),
            "max_tokens": self.config.get("MAX_TOKENS", 200),
            "stream": stream_flag
        }
        timeout_val = self.config.get("TIMEOUT", 20)

        try:
            response = requests.post(url, headers=headers, json=data, timeout=timeout_val, stream=stream_flag)
            response.raise_for_status()
        except Exception as e:
            logger.exception("DeepSeek API request failed:")
            return "[Error: API request failed]"

        # If streaming, read partial chunks
        if stream_flag:
            final_message = ""
            chunk_count = 0
            try:
                for line in response.iter_lines():
                    if self._is_empty_or_keepalive(line):
                        continue
                    chunk_count += 1

                    try:
                        json_line = json.loads(line.decode("utf-8"))
                        delta = json_line.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        final_message += delta
                        # If we got a callback, let's emit partial progress
                        if stream_progress_callback:
                            # Example approach: each chunk = +5% until near 100
                            approximate_pct = min(99, chunk_count * 5)
                            stream_progress_callback(approximate_pct)
                    except Exception as stream_e:
                        logger.exception("Error parsing a line from DeepSeek stream:")
                logger.info(f"DeepSeek streamed final content: {final_message}")
                return final_message if final_message else "[Error: Empty streamed response]"
            except Exception as e:
                logger.exception("Error reading streamed response from DeepSeek:")
                return "[Error: API request failed during streaming]"
        else:
            # Non-streaming behavior
            try:
                response_json = response.json()
            except Exception as e:
                logger.exception("Failed to decode JSON response from DeepSeek:")
                return "[Error: Unable to parse response]"
            if "choices" in response_json and response_json["choices"]:
                message = response_json["choices"][0].get("message", {}).get("content", "").strip()
                if message:
                    logger.info(f"DeepSeek API response content: {message}")
                    return message
                else:
                    logger.error(f"DeepSeek API returned empty message: {response_json}")
                    return "[Error: Empty response message]"
            else:
                logger.error(f"Invalid DeepSeek API response structure: {response_json}")
                return "[Error: Unexpected response format]"

    def send_request(self, url: str, headers: dict, data: dict) -> str:
        """General-purpose POST with retries, backoff, and API_DELAY."""
        retries = 3
        backoff_factor = 2
        timeout_val = self.config.get("TIMEOUT", 20)

        if not check_internet():
            logger.error("No internet connection.")
            showInfo("No internet connection. Please check your network and try again.")
            return "[Error: No internet]"

        for attempt in range(retries):
            try:
                safe_data = data.copy()
                # Don't log the real API key
                if "Authorization" in headers:
                    safe_data["Authorization"] = "[REDACTED]"
                logger.info(f"Sending API request (attempt {attempt+1}): {safe_data}")
                response = requests.post(url, headers=headers, json=data, timeout=timeout_val)
                response.raise_for_status()

                response_json = response.json()
                # Respect configured delay
                time.sleep(self.config.get("API_DELAY", 1))

                if "choices" in response_json and response_json["choices"]:
                    message = response_json["choices"][0].get("message", {}).get("content", "").strip()
                    if message:
                        logger.info(f"API response content: {message[:200]}...")  # log partial for brevity
                        return message
                    else:
                        logger.error(f"Empty response message: {response_json}")
                        return "[Error: Empty response message]"
                logger.error(f"Invalid API response structure: {response_json}")
                return "[Error: Unexpected response format]"

            except requests.exceptions.Timeout:
                logger.warning(f"Timeout error. Retrying attempt {attempt + 1}/{retries}...")
                time.sleep(backoff_factor * (attempt + 1))

            except requests.exceptions.RequestException as e:
                logger.exception("API error:")
                safe_show_info(f"API error: {e}")
                return "[Error: API request failed]"

        return "[Error: API request failed after multiple attempts]"

    @staticmethod
    def _is_empty_or_keepalive(line: bytes) -> bool:
        """Helper for ignoring keep-alive or empty lines in streaming."""
        if not line:
            return True
        text = line.decode("utf-8").strip()
        return not text or text.startswith("data: [DONE]") or text.startswith(":") 


# -------------------------------
# Settings Dialog
# -------------------------------
class SettingsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OmniPrompt Configuration")
        self.setMinimumWidth(500)
        self.config = None
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout()
        # AI Provider
        provider_group = QGroupBox("AI Provider Selection")
        provider_layout = QVBoxLayout()
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(AI_PROVIDERS)
        provider_layout.addWidget(QLabel("Select AI Provider:"))
        provider_layout.addWidget(self.provider_combo)

        self.model_combo = QComboBox()
        provider_layout.addWidget(QLabel("Model:"))
        provider_layout.addWidget(self.model_combo)
        provider_group.setLayout(provider_layout)
        layout.addWidget(provider_group)

        # API Settings
        api_group = QGroupBox("API Settings")
        api_layout = QFormLayout()

        self.api_key_input = QLineEdit()
        self.api_key_input.setPlaceholderText("Enter API key")
        api_layout.addRow("API Key:", self.api_key_input)

        self.temperature_input = QLineEdit()
        self.temperature_input.setValidator(QDoubleValidator(0.0, 2.0, 2))
        api_layout.addRow("Temperature:", self.temperature_input)

        self.max_tokens_input = QLineEdit()
        self.max_tokens_input.setValidator(QIntValidator(100, 4000))
        api_layout.addRow("Max Tokens:", self.max_tokens_input)

        api_group.setLayout(api_layout)
        layout.addWidget(api_group)

        # Advanced Settings
        self.advanced_button = QPushButton("Advanced Settings")
        self.advanced_button.clicked.connect(lambda: AdvancedSettingsDialog(self).exec())
        layout.addWidget(self.advanced_button)

        # View Log
        self.view_log_button = QPushButton("View Log")
        self.view_log_button.clicked.connect(self.show_log)
        layout.addWidget(self.view_log_button)

        # Save/Cancel
        button_layout = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)

        self.setLayout(layout)
        self.provider_combo.currentIndexChanged.connect(self.update_api_options)
        self.update_api_options()

    def update_api_options(self) -> None:
        provider = self.provider_combo.currentText()
        self.model_combo.clear()
        if provider == "openai":
            self.api_key_input.setPlaceholderText("Enter OpenAI API Key")
            self.model_combo.addItems(["gpt-4o-mini", "gpt-3.5-turbo", "gpt-4o", "o3-mini", "o1-mini"])
        elif provider == "deepseek":
            self.api_key_input.setPlaceholderText("Enter DeepSeek API Key")
            self.model_combo.addItems(["deepseek-chat", "deepseek-reasoner"])

    def load_config(self, config: dict) -> None:
        self.config = config
        self.provider_combo.setCurrentText(config.get("AI_PROVIDER", "openai"))
        self.update_api_options()

        if config["AI_PROVIDER"] == "openai":
            self.api_key_input.setText(config.get("OPENAI_API_KEY", ""))
            self.model_combo.setCurrentText(config.get("OPENAI_MODEL", ""))
        else:
            self.api_key_input.setText(config.get("DEEPSEEK_API_KEY", ""))
            self.model_combo.setCurrentText(config.get("DEEPSEEK_MODEL", ""))

        self.temperature_input.setText(str(config.get("TEMPERATURE", 0.2)))
        self.max_tokens_input.setText(str(config.get("MAX_TOKENS", 200)))

    def get_updated_config(self) -> dict:
        provider = self.provider_combo.currentText()
        return {
            "AI_PROVIDER": provider,
            "OPENAI_API_KEY": self.api_key_input.text() if provider == "openai" else "",
            "DEEPSEEK_API_KEY": self.api_key_input.text() if provider == "deepseek" else "",
            "OPENAI_MODEL": self.model_combo.currentText() if provider == "openai" else "",
            "DEEPSEEK_MODEL": self.model_combo.currentText() if provider == "deepseek" else "",
            "TEMPERATURE": float(self.temperature_input.text()),
            "MAX_TOKENS": int(self.max_tokens_input.text()),
            "PROMPT": self.config.get("PROMPT", ""),
            "SELECTED_FIELDS": self.config.get("SELECTED_FIELDS", {"output_field": "Output"}),
            "_version": DEFAULT_CONFIG["_version"],
            "API_DELAY": self.config.get("API_DELAY", 1),
            "TIMEOUT": self.config.get("TIMEOUT", 20),
            "DEEPSEEK_STREAM": self.config.get("DEEPSEEK_STREAM", False)
        }

    def show_log(self) -> None:
        log_path = os.path.join(os.path.dirname(__file__), "omnPrompt-anki.log")
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                log_content = f.read()
        except Exception as e:
            safe_show_info(f"Failed to load log file: {e}")
            return
        log_dialog = QDialog(self)
        log_dialog.setWindowTitle("OmniPrompt Anki Log")
        log_dialog.setMinimumSize(600, 400)
        layout = QVBoxLayout(log_dialog)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setPlainText(log_content)
        layout.addWidget(text_edit)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(log_dialog.accept)
        layout.addWidget(close_btn)

        log_dialog.exec()

# -------------------------------
# Advanced Settings Dialog
# -------------------------------
class AdvancedSettingsDialog(QDialog):
    """Lets user set additional fields like API_DELAY, TIMEOUT, or streaming flags."""
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Advanced Settings")
        self.setMinimumWidth(400)
        # Access the global manager's config
        self.config = omni_prompt_manager.config
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout()
        form_layout = QFormLayout()

        # API Delay
        self.api_delay_input = QLineEdit()
        self.api_delay_input.setValidator(QDoubleValidator(0.0, 60.0, 2, self))
        self.api_delay_input.setText(str(self.config.get("API_DELAY", 1)))
        form_layout.addRow("API Delay (seconds):", self.api_delay_input)

        # API Timeout
        self.timeout_input = QLineEdit()
        self.timeout_input.setValidator(QDoubleValidator(0.0, 300.0, 1, self))
        self.timeout_input.setText(str(self.config.get("TIMEOUT", 20)))
        form_layout.addRow("API Timeout (seconds):", self.timeout_input)

        # DeepSeek Stream?
        self.deepseek_stream_checkbox = QComboBox()
        self.deepseek_stream_checkbox.addItems(["False", "True"])
        initial_stream_val = "True" if self.config.get("DEEPSEEK_STREAM", False) else "False"
        self.deepseek_stream_checkbox.setCurrentText(initial_stream_val)
        form_layout.addRow("DeepSeek Streaming:", self.deepseek_stream_checkbox)

        layout.addLayout(form_layout)

        # Buttons
        button_layout = QHBoxLayout()
        save_button = QPushButton("Save")
        save_button.clicked.connect(self.accept)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(save_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)

        self.setLayout(layout)

    def accept(self) -> None:
        try:
            delay = float(self.api_delay_input.text())
            timeout_val = float(self.timeout_input.text())
        except ValueError:
            safe_show_info("Invalid input for advanced settings.")
            return

        self.config["API_DELAY"] = delay
        self.config["TIMEOUT"] = timeout_val
        self.config["DEEPSEEK_STREAM"] = (self.deepseek_stream_checkbox.currentText() == "True")

        omni_prompt_manager.save_config()
        super().accept()

# -------------------------------
# Update with OmniPrompt Dialog
# -------------------------------
class UpdateOmniPromptDialog(QDialog):
    def __init__(self, notes: list, manager: OmniPromptManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update with OmniPrompt")
        self.notes = notes
        self.manager = manager
        self.worker = None
        self.setup_ui()

    def setup_ui(self):
        main_layout = QHBoxLayout(self)
        left_panel = QVBoxLayout()

        # "Saved Prompts:" label + combo
        left_panel.addWidget(QLabel("Saved Prompts:"))
        self.prompt_combo = QComboBox()
        self.prompt_combo.setEditable(True)
        self.prompt_combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if self.prompt_combo.lineEdit():
            self.prompt_combo.lineEdit().setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        left_panel.addWidget(self.prompt_combo)

        # "Prompt Template:" label + text editor
        left_panel.addWidget(QLabel("Prompt Template:"))
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setAcceptRichText(False)
        self.prompt_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.prompt_edit.setPlainText(self.manager.config.get("PROMPT", ""))
        left_panel.addWidget(self.prompt_edit)

        # Save prompt button
        self.save_prompt_button = QPushButton("Save Current Prompt")
        left_panel.addWidget(self.save_prompt_button)

        # "Output Field:" label + combo
        left_panel.addWidget(QLabel("Output Field:"))
        self.output_field_combo = QComboBox()
        if self.notes:
            first_note = self.notes[0]
            model = mw.col.models.get(first_note.mid)
            if model:
                fields = mw.col.models.field_names(model)
                self.output_field_combo.addItems(fields)
        left_panel.addWidget(self.output_field_combo)

        # Start + Stop + Save Manual Edits
        self.start_button = QPushButton("Start")
        left_panel.addWidget(self.start_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        left_panel.addWidget(self.stop_button)

        self.save_changes_button = QPushButton("Save Manual Edits")
        left_panel.addWidget(self.save_changes_button)

        main_layout.addLayout(left_panel, 1)

        # Right panel: table with 3 columns
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Progress", "Original", "Generated"])
        self.table.horizontalHeader().setStretchLastSection(True)
        main_layout.addWidget(self.table, 3)

        self.setLayout(main_layout)

        # -------------
        # Connect signals *after* text editor is created
        # -------------
        self.load_prompts()
        # Manually load the currently selected prompt (if any)
        current_text = self.prompt_combo.currentText()
        if current_text:
            self.load_selected_prompt(current_text)
        # Now connect the combo
        self.prompt_combo.currentTextChanged.connect(self.load_selected_prompt)

        self.save_prompt_button.clicked.connect(self.save_current_prompt)
        self.start_button.clicked.connect(self.start_processing)
        self.stop_button.clicked.connect(self.stop_processing)
        self.save_changes_button.clicked.connect(self.save_manual_edits)

        # Shortcut for "start" = Ctrl+Return
        start_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        start_shortcut.activated.connect(self.start_processing)

        # Focus the combo when the dialog opens
        self.prompt_combo.setFocus()

    def load_prompts(self):
        self.prompt_combo.clear()
        prompts = load_prompt_templates()
        for name in prompts.keys():
            self.prompt_combo.addItem(name)

    def load_selected_prompt(self, text: str):
        prompts = load_prompt_templates()
        if text in prompts:
            self.prompt_edit.setPlainText(prompts[text])

    def save_current_prompt(self):
        name, ok = getText("Enter a name for the prompt:")
        if ok and name:
            prompts = load_prompt_templates()
            prompts[name] = self.prompt_edit.toPlainText()
            save_prompt_templates(prompts)
            self.load_prompts()
            self.prompt_combo.setCurrentText(name)
            showInfo("Prompt saved.")

    def start_processing(self):
        note_prompts = []
        prompt_template = self.prompt_edit.toPlainText()
        output_field = self.output_field_combo.currentText().strip()
        if not output_field:
            safe_show_info("Please select an output field.")
            return

        for note in self.notes:
            try:
                # Attempt to fill placeholders from note fields
                formatted_prompt = prompt_template.format(**note)
            except KeyError as e:
                safe_show_info(f"Missing field {e} in note {note.id}")
                continue
            note_prompts.append((note, formatted_prompt))

        if not note_prompts:
            safe_show_info("No valid notes to process.")
            return

        self.table.setRowCount(len(note_prompts))
        for row, (note, _) in enumerate(note_prompts):
            progress_item = QTableWidgetItem("0%")
            try:
                original_text = note[output_field]
            except Exception:
                original_text = ""

            original_item = QTableWidgetItem(original_text)
            original_item.setData(Qt.ItemDataRole.UserRole, note.id)
            generated_item = QTableWidgetItem("")

            self.table.setItem(row, 0, progress_item)
            self.table.setItem(row, 1, original_item)
            self.table.setItem(row, 2, generated_item)

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        # Create the worker
        self.worker = NoteProcessingWorker(
            note_prompts,
            self._generate_with_progress
        )
        self.worker.progress_update.connect(self.update_progress_cell, Qt.ConnectionType.QueuedConnection)
        self.worker.note_result.connect(self.update_note_result, Qt.ConnectionType.QueuedConnection)
        self.worker.finished_processing.connect(self.processing_finished, Qt.ConnectionType.QueuedConnection)

        self.worker.start()

    def stop_processing(self):
        if self.worker:
            self.worker.cancel()
            self.stop_button.setEnabled(False)

    def _generate_with_progress(self, prompt, stream_progress_callback=None):
        """Just a pass-through to our manager's generate_ai_response."""
        return self.manager.generate_ai_response(prompt, stream_progress_callback=stream_progress_callback)

    def update_progress_cell(self, row_index: int, pct: int):
        """Slot that receives partial progress from the worker."""
        item = self.table.item(row_index, 0)
        if item:
            item.setText(f"{pct}%")

    def update_note_result(self, note, explanation: str):
        output_field = self.output_field_combo.currentText().strip()

        # Find the note in the table
        for row in range(self.table.rowCount()):
            original_item = self.table.item(row, 1)
            if original_item and original_item.data(Qt.ItemDataRole.UserRole) == note.id:
                # Set final progress to 100% if not already
                progress_item = self.table.item(row, 0)
                if progress_item:
                    progress_item.setText("100%")

                self.table.item(row, 2).setText(explanation)
                try:
                    note[output_field] = explanation
                    mw.col.update_note(note)
                except Exception as e:
                    logger.exception(f"Error updating note {note.id}: {e}")
                break

    def save_manual_edits(self):
        output_field = self.output_field_combo.currentText().strip()
        for row in range(self.table.rowCount()):
            original_item = self.table.item(row, 1)
            generated_item = self.table.item(row, 2)
            note_id = original_item.data(Qt.ItemDataRole.UserRole)
            note = mw.col.get_note(note_id)
            new_text = generated_item.text()
            try:
                note[output_field] = new_text
                mw.col.update_note(note)
            except Exception as e:
                logger.exception(f"Error saving manual edit for note {note.id}: {e}")
        safe_show_info("Manual edits saved.")

    def processing_finished(self, processed: int, total: int, worker_error_count: int):
        safe_show_info(f"Processing finished: {processed}/{total} notes processed with {worker_error_count} errors.")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

# -------------------------------
# About Dialog
# -------------------------------
class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About OmniPrompt Anki")
        layout = QVBoxLayout(self)
        about_text = (
            "<h2>OmniPrompt Anki Add‑on</h2>"
            "<p>Version: 1.1.2</p>"
            "<p><a href='https://ankiweb.net/shared/review/1383162606'>Rate add-on on AnkiWeb</a></p>"
            "<p>For documentation, visit:</p>"
            "<p><a href='https://github.com/stanamosov/omniprompt-anki'>GitHub Repository</a></p>"
            "<p><a href='https://codeberg.org/stanamosov/omniprompt-anki'>Codeberg Repository</a></p>"
            "<p>Credits: Stanislav Amosov</p>"
            "<p>Contact: <a href=\"mailto:omniprompt@mailwizard.org\">omniprompt@mailwizard.org</a></p>"
        )
        label = QLabel(about_text)
        label.setOpenExternalLinks(True)
        layout.addWidget(label)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

        self.setLayout(layout)

# -------------------------------
# Tools Menu
# -------------------------------
def setup_omniprompt_menu():
    tools_menu = mw.form.menuTools
    omni_menu = QMenu("OmniPrompt", mw)

    settings_action = QAction("Settings", mw)
    settings_action.triggered.connect(lambda: omni_prompt_manager.show_settings_dialog())
    omni_menu.addAction(settings_action)

    about_action = QAction("About", mw)
    about_action.triggered.connect(lambda: AboutDialog(mw).exec())
    omni_menu.addAction(about_action)

    tools_menu.addMenu(omni_menu)

# -------------------------------
# Browser Context Menu Hook
# -------------------------------
def on_browser_context_menu(browser: Browser, menu):
    note_ids = browser.selectedNotes()
    if note_ids:
        action = QAction("Update with OmniPrompt", browser)
        action.triggered.connect(lambda: update_notes_with_omniprompt(note_ids))
        menu.addAction(action)

gui_hooks.browser_will_show_context_menu.append(on_browser_context_menu)

def update_notes_with_omniprompt(note_ids: list):
    notes = [mw.col.get_note(nid) for nid in note_ids]
    dialog = UpdateOmniPromptDialog(notes, omni_prompt_manager, parent=mw)
    dialog.exec()

# -------------------------------
# Instantiate the Add‑on & Setup
# -------------------------------
omni_prompt_manager = OmniPromptManager()
setup_omniprompt_menu()

# Global shortcut for "Update with OmniPrompt" (Ctrl+Shift+O or Meta+Shift+O)
def shortcut_update_notes():
    logger.info("Global shortcut activated.")
    browser = mw.app.activeWindow()
    if isinstance(browser, Browser):
        note_ids = browser.selectedNotes()
        if note_ids:
            update_notes_with_omniprompt(note_ids)
        else:
            showInfo("No notes selected in the browser.")
    else:
        showInfo("Browser not available.")
    print("Shortcut activated!")

# Install both shortcuts for cross-platform:
shortcut_ctrl = QShortcut(QKeySequence("Ctrl+Shift+O"), mw)
shortcut_ctrl.setContext(Qt.ShortcutContext.ApplicationShortcut)
shortcut_ctrl.activated.connect(shortcut_update_notes)

shortcut_meta = QShortcut(QKeySequence("Meta+Shift+O"), mw)
shortcut_meta.setContext(Qt.ShortcutContext.ApplicationShortcut)
shortcut_meta.activated.connect(shortcut_update_notes)
