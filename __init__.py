import requests
import logging
import os
import time
import socket
import sys
import json
from jsonschema import validate
from anki.errors import NotFoundError 
from aqt.utils import showInfo
from PyQt6.QtCore import QTimer

from aqt import mw
from aqt.qt import (
    QAction, QDialog, QVBoxLayout, QGroupBox, QComboBox, QLabel,
    QLineEdit, QDoubleValidator, QIntValidator, QFormLayout,
    QPushButton, QTextEdit, QHBoxLayout, QMessageBox, QProgressDialog, Qt
)
from aqt.browser import Browser
from anki.hooks import addHook
from aqt.utils import showInfo, getText
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QMetaObject
from PyQt6.QtGui import QTextOption
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
    "OPENAI_TEMPERATURE": 0.2,
    "DEEPSEEK_TEMPERATURE": 0.2,
    "OPENAI_MAX_TOKENS": 200,
    "DEEPSEEK_MAX_TOKENS": 200,
    "note_type_id": None,
    "PROMPT": "Paste your prompt here.",
    "SELECTED_FIELDS": {
        "output_field": "Output"
    }
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
        "OPENAI_TEMPERATURE": {"type": "number"},
        "DEEPSEEK_TEMPERATURE": {"type": "number"},
        "OPENAI_MAX_TOKENS": {"type": "integer"},
        "DEEPSEEK_MAX_TOKENS": {"type": "integer"},
        "note_type_id": {"type": ["number", "null"]},
        "PROMPT": {"type": "string"},
        "SELECTED_FIELDS": {
            "type": "object",
            "properties": {
                "output_field": {"type": "string"}
            }
        }
    },
    "required": ["AI_PROVIDER", "note_type_id"]
}


# -------------------------------
# Helper Functions for Prompts
# -------------------------------
def safe_show_info(message: str) -> None:
    """
    Invokes showInfo(message) on the main (GUI) thread using QTimer.singleShot.
    This ensures the UI call is executed in the proper thread.
    """
    QTimer.singleShot(0, lambda: showInfo(message))


def load_prompt_templates() -> dict:
    """
    Load prompt templates from a file.
    """
    templates_path = os.path.join(os.path.dirname(__file__), "prompt_templates.txt")
    templates = {}

    if os.path.exists(templates_path):
        with open(templates_path, "r", encoding="utf-8") as file:
            current_key = None
            current_value = []
            for line in file:
                # Only remove trailing newlines, preserve all other whitespace
                line = line.rstrip('\n')
                if line.startswith("[") and line.endswith("]"):
                    if current_key is not None:
                        templates[current_key] = "\n".join(current_value)
                    current_key = line[1:-1]
                    current_value = []
                else:
                    current_value.append(line)
            if current_key is not None:
                templates[current_key] = "\n".join(current_value)
    return templates


def save_prompt_templates(templates: dict) -> None:
    """
    Save prompt templates to a file.
    """
    templates_path = os.path.join(os.path.dirname(__file__), "prompt_templates.txt")
    os.makedirs(os.path.dirname(templates_path), exist_ok=True)
    with open(templates_path, "w", encoding="utf-8", newline="\n") as file:
        for key, value in sorted(templates.items()):
            file.write(f"[{key}]\n{value}\n\n")


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
    base = os.path.basename(raw_dir).strip()  # remove extra whitespace from the base name
    return os.path.join(parent, base)

def setup_logger() -> logging.Logger:
    logger = logging.getLogger("OmniPromptAnki")
    logger.setLevel(logging.INFO)

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

    if not logger.handlers:
        logger.addHandler(handler)

    return logger


class SafeAnkiRotatingFileHandler(RotatingFileHandler):
    """Custom handler for Anki environment compatibility"""
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
    log_path = os.path.join(
        mw.addonManager.addonsFolder(),
        "omniprompt-anki",
        "omnPrompt-anki.log"
    )
    try:
        size = os.path.getsize(log_path)
        if size > 4.5 * 1024 * 1024:  # 4.5MB warning
            print("Log file approaching maximum size")
    except Exception:
        pass


# Run check periodically (every 100 card updates)
addHook("reset", check_log_size)

# Initialize logger at module level
logger = setup_logger()


# -------------------------------
# Background Worker for Note Processing
# -------------------------------
class NoteProcessingWorker(QThread):
    progress_update = pyqtSignal(int)       # emits current progress (number of notes processed)
    note_result = pyqtSignal(object, str)     # emits (note object, explanation)
    error_occurred = pyqtSignal(object, str)  # emits (note object, error message)
    finished_processing = pyqtSignal(int, int, int)  # emits (processed, total, error_count)

    def __init__(self, note_prompts: list[tuple], generate_ai_response_callback, parent=None):
        """
        :param note_prompts: List of tuples (note, prompt)
        :param generate_ai_response_callback: Callable that takes a prompt and returns a response string.
        """
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
            try:
                explanation = self.generate_ai_response_callback(prompt)
                self.note_result.emit(note, explanation)
            except Exception as e:
                self.error_count += 1
                logger.exception(f"Error processing note {note.id}")
                self.error_occurred.emit(note, str(e))
            self.processed += 1
            self.progress_update.emit(i + 1)
        self.finished_processing.emit(self.processed, total, self.error_count)

    def cancel(self) -> None:
        self._is_cancelled = True

# -------------------------------
# Main Add-on Class
# -------------------------------
class GPTGrammarExplainer:
    @property
    def addon_dir(self) -> str:
        return os.path.dirname(__file__)

    def __init__(self):
        self.logger = logging.getLogger("OmniPromptAnki")
        self.config = self.load_config()
        addHook("browser.setupMenus", self.on_browser_will_show)
        mw.addonManager.setConfigAction(__name__, self.show_settings_dialog)

    def save_config(self) -> None:
        try:
            validated = self.validate_config(self.config)
            migrated = self.migrate_config(validated)
            if migrated.get("_version") != DEFAULT_CONFIG["_version"]:
                self.emergency_log_cleanup()
                showInfo("Configuration version mismatch. Reset to defaults.")
                return
            mw.addonManager.writeConfig(__name__, migrated)
        except Exception as e:
            self.logger.exception(f"Config save failed: {str(e)}")
            self.restore_config()

    def emergency_log_cleanup(self) -> None:
        """Safer log recovery with fallbacks"""
        try:
            for handler in self.logger.handlers[:]:
                self.logger.removeHandler(handler)
                handler.close()
            log_path = os.path.join(self.addon_dir, "omnPrompt-anki.log")
            with open(log_path, 'w') as f:
                f.truncate()
            new_handler = SafeAnkiRotatingFileHandler(
                filename=log_path,
                mode='a',
                maxBytes=5 * 1024 * 1024,
                backupCount=2,
                encoding='utf-8',
                delay=True
            )
            new_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
            self.logger.addHandler(new_handler)
        except Exception as e:
            print(f"Emergency cleanup failed: {str(e)}")
            self.logger.addHandler(logging.StreamHandler(sys.stdout))

    def make_openai_request(self, prompt: str) -> str:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config['OPENAI_API_KEY']}"
        }
        data = {
            "model": self.config["OPENAI_MODEL"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.config["OPENAI_MAX_TOKENS"],
            "temperature": self.config["OPENAI_TEMPERATURE"]
        }
        return self.send_request(url, headers, data)

    def make_deepseek_request(self, prompt: str) -> str:
        """
        Constructs and sends a request to DeepSeek's Chat Completions API.
        Supports both non-streaming and streaming responses based on configuration.
        
        Official (non-streaming) example:
        curl https://api.deepseek.com/chat/completions \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer <DeepSeek API Key>" \
            -d '{
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Hello!"}
                ],
                "stream": false
                }'
        
        If streaming is enabled, the response is expected to be sent as a stream.
        """
        url = "https://api.deepseek.com/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config['DEEPSEEK_API_KEY']}"
        }
        
        # Use a new config parameter to control streaming (default: False)
        stream_flag = self.config.get("DEEPSEEK_STREAM", False)
        
        # For streaming mode, you might want to include a system message if desired.
        # Here we include a default system message; adjust as needed.
        data = {
            "model": self.config["DEEPSEEK_MODEL"],
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            "temperature": self.config.get("DEEPSEEK_TEMPERATURE", 0.2),
            "max_tokens": self.config.get("DEEPSEEK_MAX_TOKENS", 200),
            "stream": stream_flag
        }
        
        timeout = 20  # seconds
        try:
            response = requests.post(url, headers=headers, json=data, timeout=timeout, stream=stream_flag)
            response.raise_for_status()
        except Exception as e:
            logger.exception("DeepSeek API request failed:")
            return "[Error: API request failed]"
        
        # If streaming is enabled, process the streamed response.
        if stream_flag:
            final_message = ""
            try:
                # Iterate over each line in the streamed response.
                for line in response.iter_lines():
                    if line:
                        try:
                            # Each line is expected to be a JSON object.
                            json_line = json.loads(line.decode("utf-8"))
                            # Depending on DeepSeek's streaming format, you might need to adjust this.
                            # For example, if the streaming response returns partial deltas:
                            delta = json_line.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            final_message += delta
                        except Exception as stream_e:
                            logger.exception("Error parsing a line from DeepSeek stream:")
                logger.info(f"DeepSeek streamed API response content: {final_message}")
                return final_message if final_message else "[Error: Empty streamed response]"
            except Exception as e:
                logger.exception("Error reading streamed response from DeepSeek:")
                return "[Error: API request failed during streaming]"
        else:
            # Non-streaming mode: attempt to parse the full JSON response.
            try:
                response_json = response.json()
            except Exception as e:
                logger.exception("Failed to decode JSON response from DeepSeek:")
                return "[Error: Unable to parse response]"
            
            # Check for a response structure similar to OpenAI's (adjust as needed)
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
        """Generalized API request function with retry mechanism"""
        retries = 3
        backoff_factor = 2
        timeout = 20

        if not check_internet():
            logger.error("No internet connection.")
            showInfo("No internet connection. Please check your network and try again.")
            return "[Error: No internet]"

        for attempt in range(retries):
            try:
                safe_data = data.copy()
                safe_data["Authorization"] = "[REDACTED]"
                logger.info(f"Sending API request: {safe_data}")
                response = requests.post(url, headers=headers, json=data, timeout=timeout)
                response.raise_for_status()
                response_json = response.json()
                if "choices" in response_json and response_json["choices"]:
                    message = response_json["choices"][0].get("message", {}).get("content", "").strip()
                    if message:
                        logger.info(f"API response content: {message}")
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

    def generate_ai_response(self, prompt: str) -> str:
        provider = self.config.get("AI_PROVIDER", "openai")
        if provider == "openai":
            return self.make_openai_request(prompt)
        elif provider == "deepseek":
            if not self.config.get("DEEPSEEK_MODEL"):
                return "[Error: No DeepSeek model selected]"
            return self.make_deepseek_request(prompt)
        else:
            logger.error(f"Invalid AI provider: {provider}")
            return "[Error: Invalid AI provider]"

    def show_settings_dialog(self) -> None:
        dialog = SettingsDialog(mw)
        dialog.load_config(self.config)
        if dialog.exec():
            self.config = dialog.get_updated_config()
            self.save_config()

    def load_config(self) -> dict:
        raw_config = mw.addonManager.getConfig(__name__) or {}
        validated = self.validate_config(raw_config)
        return self.migrate_config(validated)

    def migrate_config(self, config: dict) -> dict:
        # Log migration steps
        if config.get("_version", 0) < DEFAULT_CONFIG["_version"]:
            self.logger.info(f"Migrating config from version {config.get('_version', 'unknown')} to {DEFAULT_CONFIG['_version']}")
        migrated = DEFAULT_CONFIG.copy()
        migrated.update(config)
        if migrated["_version"] < 1.1:
            migrated.setdefault("SELECTED_FIELDS", DEFAULT_CONFIG["SELECTED_FIELDS"])
            migrated["_version"] = 1.1
        return migrated

    def validate_config(self, config: dict) -> dict:
        try:
            validate(instance=config, schema=CONFIG_SCHEMA)
            return config
        except Exception as e:
            self.logger.exception(f"Config validation error: {str(e)}")
            self.logger.info("Reverting to default configuration")
            return DEFAULT_CONFIG.copy()

    def backup_config(self) -> None:
        backup_path = os.path.join(self.addon_dir, "config_backup.json")
        with open(backup_path, "w") as f:
            json.dump(self.config, f)

    def restore_config(self) -> None:
        backup_path = os.path.join(self.addon_dir, "config_backup.json")
        if os.path.exists(backup_path):
            with open(backup_path, "r") as f:
                self.config = json.load(f)
            self.save_config()

    def save_config(self) -> None:
        try:
            validated = self.validate_config(self.config)
            validated = self.migrate_config(validated)
            if validated.get("_version") != DEFAULT_CONFIG["_version"]:
                self.emergency_log_cleanup()
                showInfo("Configuration version mismatch. Reset to defaults.")
                return
            mw.addonManager.writeConfig(__name__, validated)
        except Exception as e:
            self.logger.exception(f"Config save failed: {str(e)}")
            self.restore_config()

    def on_browser_will_show(self, browser: Browser) -> None:
        menu = browser.form.menuEdit
        self.action = QAction("Update cards with OmniPrompt", browser)
        self.action.triggered.connect(lambda: self.update_selected_notes(browser))
        menu.addAction(self.action)

    def update_selected_notes(self, browser: Browser) -> None:
        selected_notes = list(set(browser.selectedNotes()))
        note_type_id = self.config.get("note_type_id", None)

        if note_type_id is None:
            showInfo("No note type selected! Please configure the note type in settings.")
            return

        target_field, ok = getText(
            "Enter the field name where the generated data should be saved:",
            default=self.config.get("SELECTED_FIELDS", {}).get("output_field", "Output")
        )
        if not ok:
            return

        self.config["SELECTED_FIELDS"]["output_field"] = target_field

        # Prepare list of (note, prompt) for valid notes.
        note_prompts: list[tuple] = []
        for note_id in selected_notes:
            note = mw.col.get_note(note_id)
            if note.note_type()['id'] != note_type_id:
                continue
            try:
                prompt = self.config["PROMPT"].format(**note)
            except KeyError as e:
                showInfo(f"Missing field {e} in note {note.id}")
                self.logger.error(f"Missing field {e} in note {note.id}")
                continue
            note_prompts.append((note, prompt))

        if not note_prompts:
            showInfo("No valid notes found for processing.")
            return

        progress = QProgressDialog("Updating cards with OmniPrompt...", "Cancel", 0, len(note_prompts), mw)
        progress.setWindowTitle("OmniPrompt Processing")
        progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.WindowModality.WindowModal)

        modified_fields_counter = 0
        error_count_counter = 0

        def on_progress_update(value: int) -> None:
            progress.setValue(value)

        def on_note_result(note, explanation: str) -> None:
            nonlocal modified_fields_counter
            try:
                # Update the note object directly
                if note[target_field] != explanation:
                    note[target_field] = explanation
                    mw.col.update_note(note)
                    modified_fields_counter += 1
                self.logger.info(f"Processed note {note.id}")
            except Exception as ex:
                self.logger.exception(f"Error updating note {note.id}: {ex}")

        def on_error_occurred(note, error_message: str) -> None:
            nonlocal error_count_counter
            error_count_counter += 1
            self.logger.error(f"Error processing note {note.id}: {error_message}")
            if error_count_counter <= 5:
                safe_show_info(f"Error processing note {note.id}: {error_message}")

        def on_finished(processed: int, total: int, worker_error_count: int) -> None:
            progress.setValue(total)
            total_errors = error_count_counter + worker_error_count
            if total_errors > 5:
                showInfo(f"{total_errors - 5} additional errors occurred. Check logs for details.")
            if processed:
                QMessageBox.information(
                    mw, "Complete",
                    f"Updated {processed}/{total} notes.\nModified {modified_fields_counter} fields."
                )
            else:
                QMessageBox.warning(mw, "Error", "No notes processed. Check fields/config.")

        # Create and start the worker thread.
        worker = NoteProcessingWorker(note_prompts, self.generate_ai_response)
        # You can keep or remove forced queued connections if needed.
        worker.progress_update.connect(on_progress_update, Qt.ConnectionType.QueuedConnection)
        worker.note_result.connect(on_note_result, Qt.ConnectionType.QueuedConnection)
        worker.error_occurred.connect(on_error_occurred, Qt.ConnectionType.QueuedConnection)
        worker.finished_processing.connect(on_finished, Qt.ConnectionType.QueuedConnection)

        def on_cancel() -> None:
            worker.cancel()
        progress.canceled.connect(on_cancel)

        worker.start()


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
        # Connect the provider combo signal
        self.provider_combo.currentIndexChanged.connect(self.update_api_options)

    def init_ui(self) -> None:
        layout = QVBoxLayout()

        # AI Provider Selection
        provider_group = QGroupBox("AI Provider Selection")
        provider_layout = QVBoxLayout()

        self.provider_combo = QComboBox()
        self.provider_combo.addItems(AI_PROVIDERS)

        provider_layout.addWidget(QLabel("Select AI Provider:"))
        provider_layout.addWidget(self.provider_combo)
        self.model_combo = QComboBox()
        provider_group.setLayout(provider_layout)
        layout.addWidget(provider_group)

        # Note Type Selection
        type_group = QGroupBox("Note Type Selection")
        type_layout = QFormLayout()
        self.note_type_combo = QComboBox()
        type_layout.addRow("Note Type:", self.note_type_combo)
        type_group.setLayout(type_layout)
        layout.addWidget(type_group)

        # API Settings
        api_group = QGroupBox("API Settings")
        api_layout = QFormLayout()

        self.api_key_input = QLineEdit()
        self.api_key_input.setPlaceholderText("Enter API key")
        api_layout.addRow("API Key:", self.api_key_input)

        self.model_combo = QComboBox()
        api_layout.addRow("Model:", self.model_combo)

        self.temperature_input = QLineEdit()
        self.temperature_input.setValidator(QDoubleValidator(0.0, 2.0, 2))
        api_layout.addRow("Temperature:", self.temperature_input)

        self.max_tokens_input = QLineEdit()
        self.max_tokens_input.setValidator(QIntValidator(100, 4000))
        api_layout.addRow("Max Tokens:", self.max_tokens_input)

        api_group.setLayout(api_layout)
        layout.addWidget(api_group)

        # Field Settings
        field_group = QGroupBox("Note Fields")
        field_layout = QFormLayout()

        self.explanation_field_combo = QComboBox()
        field_layout.addRow("Output Field:", self.explanation_field_combo)

        field_group.setLayout(field_layout)
        layout.addWidget(field_group)

        # Prompt Management
        prompt_group = QGroupBox("Prompt Templates")
        prompt_layout = QVBoxLayout()

        self.prompt_combo = QComboBox()
        self.prompt_combo.setEditable(True)
        prompt_layout.addWidget(QLabel("Saved Prompts:"))
        prompt_layout.addWidget(self.prompt_combo)

        self.save_prompt_button = QPushButton("Save Current Prompt")
        self.delete_prompt_button = QPushButton("Delete Selected Prompt")
        prompt_layout.addWidget(self.save_prompt_button)
        prompt_layout.addWidget(self.delete_prompt_button)

        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText("Use Shift+Enter for new lines")
        self.prompt_edit.setAcceptRichText(False)
        self.prompt_edit.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self.prompt_edit.document().setDocumentMargin(12)
        prompt_layout.addWidget(QLabel("Prompt Template:"))
        prompt_layout.addWidget(self.prompt_edit)

        prompt_group.setLayout(prompt_layout)
        layout.addWidget(prompt_group)

        # --- Show Log Button ---
        self.show_log_button = QPushButton("Show Log")
        self.show_log_button.clicked.connect(self.show_log)
        layout.addWidget(self.show_log_button)

        # Buttons
        button_layout = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)

        self.setLayout(layout)

        # Event Listeners
        self.note_type_combo.currentIndexChanged.connect(self.load_fields_for_selected_note_type)
        self.save_prompt_button.clicked.connect(self.save_prompt)
        self.delete_prompt_button.clicked.connect(self.delete_prompt)
        self.provider_combo.currentIndexChanged.connect(self.update_api_options)
        self.prompt_combo.currentIndexChanged.connect(self.update_prompt_from_template)

        self.load_prompts()

    def update_api_options(self) -> None:
        provider = self.provider_combo.currentText()
        self.model_combo.clear()
        if provider == "openai":
            self.api_key_input.setPlaceholderText("Enter OpenAI API Key")
            self.model_combo.addItems(["gpt-4o-mini", "gpt-3.5-turbo", "gpt-4o"])
        elif provider == "deepseek":
            self.api_key_input.setPlaceholderText("Enter DeepSeek API Key")
            self.model_combo.addItems(["deepseek-chat", "deepseek-reasoner"])

    def update_prompt_from_template(self) -> None:
        selected_template = self.prompt_combo.currentText()
        templates = load_prompt_templates()
        if selected_template in templates:
            self.prompt_edit.setPlainText(templates[selected_template])

    def load_config(self, config: dict) -> None:
        self.config = config
        self.provider_combo.setCurrentText(self.config["AI_PROVIDER"])
        
        self.update_api_options()

        if self.config["AI_PROVIDER"] == "openai":
            self.api_key_input.setText(self.config.get("OPENAI_API_KEY", ""))
            # Now the model_combo has been populated; set the current model if present
            self.model_combo.setCurrentText(self.config.get("OPENAI_MODEL", ""))
            self.temperature_input.setText(str(self.config.get("OPENAI_TEMPERATURE", 0.2)))
            self.max_tokens_input.setText(str(self.config.get("OPENAI_MAX_TOKENS", 200)))
        else:
            self.api_key_input.setText(self.config.get("DEEPSEEK_API_KEY", ""))
            self.model_combo.setCurrentText(self.config.get("DEEPSEEK_MODEL", ""))
            self.temperature_input.setText(str(self.config.get("DEEPSEEK_TEMPERATURE", 0.2)))
            self.max_tokens_input.setText(str(self.config.get("DEEPSEEK_MAX_TOKENS", 200)))

        self.note_type_combo.clear()
        for model in mw.col.models.all():
            self.note_type_combo.addItem(model['name'], userData=model['id'])
        current_id = self.config.get("note_type_id")
        if current_id:
            index = self.note_type_combo.findData(current_id)
            if index >= 0:
                self.note_type_combo.setCurrentIndex(index)
        self.load_fields_for_selected_note_type()
        self.load_prompts()
        self.prompt_edit.setPlainText(self.config.get("PROMPT", ""))
        self.update_prompt_from_template()

    def load_fields_for_selected_note_type(self) -> None:
        model_id = self.note_type_combo.currentData()
        if model_id:
            note_type = mw.col.models.get(model_id)
            if note_type:
                fields = mw.col.models.field_names(note_type)
                self.explanation_field_combo.clear()
                self.explanation_field_combo.addItems(fields)
                current_output = self.config["SELECTED_FIELDS"].get("output_field", "")
                if current_output in fields:
                    self.explanation_field_combo.setCurrentText(current_output)

    def load_prompts(self) -> None:
        self.prompt_combo.clear()
        prompts = load_prompt_templates()
        for name in prompts.keys():
            self.prompt_combo.addItem(name)

    def save_prompt(self) -> None:
        name, ok = getText("Enter a name for the prompt:")
        if ok and name:
            prompts = load_prompt_templates()
            prompts[name] = self.prompt_edit.toPlainText()
            save_prompt_templates(prompts)
            self.prompt_combo.setCurrentText(name)
            self.load_prompts()

    def delete_prompt(self) -> None:
        name = self.prompt_combo.currentText()
        prompts = load_prompt_templates()
        if name in prompts:
            del prompts[name]
            save_prompt_templates(prompts)
            self.load_prompts()
            self.prompt_edit.clear()

    def get_updated_config(self) -> dict:
        selected_note_type_index = self.note_type_combo.currentIndex()
        selected_note_type_id = self.note_type_combo.itemData(selected_note_type_index)
        return {
            "AI_PROVIDER": self.provider_combo.currentText(),
            "OPENAI_API_KEY": self.api_key_input.text() if self.provider_combo.currentText() == "openai" else "",
            "DEEPSEEK_API_KEY": self.api_key_input.text() if self.provider_combo.currentText() == "deepseek" else "",
            "OPENAI_MODEL": self.model_combo.currentText() if self.provider_combo.currentText() == "openai" else "",
            "DEEPSEEK_MODEL": self.model_combo.currentText() if self.provider_combo.currentText() == "deepseek" else "",
            "OPENAI_TEMPERATURE": float(self.temperature_input.text()) if self.provider_combo.currentText() == "openai" else self.config["OPENAI_TEMPERATURE"],
            "DEEPSEEK_TEMPERATURE": float(self.temperature_input.text()) if self.provider_combo.currentText() == "deepseek" else self.config["DEEPSEEK_TEMPERATURE"],
            "OPENAI_MAX_TOKENS": int(self.max_tokens_input.text()) if self.provider_combo.currentText() == "openai" else self.config["OPENAI_MAX_TOKENS"],
            "DEEPSEEK_MAX_TOKENS": int(self.max_tokens_input.text()) if self.provider_combo.currentText() == "deepseek" else self.config["DEEPSEEK_MAX_TOKENS"],
            "note_type_id": selected_note_type_id,
            "PROMPT": self.prompt_edit.toPlainText(),
            "SELECTED_FIELDS": {
                "output_field": self.explanation_field_combo.currentText()
            }
        }
    
    def show_log(self) -> None:
        """
        Opens a dialog that displays the contents of the add-on's log file.
        """
        # Determine the log file path
        log_path = os.path.join(os.path.dirname(__file__), "omnPrompt-anki.log")
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                log_content = f.read()
        except Exception as e:
            safe_show_info(f"Failed to load log file: {e}")
            return

        # Create a dialog to display the log
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
# Instantiate the Add-on
# -------------------------------
gpt_grammar_explainer = GPTGrammarExplainer()
