"""
OmniPrompt Anki Add‑on

Features:
- A Tools menu entry “OmniPrompt” (as a submenu inside the Tools menu) with “Settings” and “About” items.
- A browser context‑menu action “Update with OmniPrompt” (appearing on right‑click on a note).
- A Settings dialog that lets the user set AI provider settings (API key, temperature, max tokens) and view the log.
- An Update dialog that opens when “Update with OmniPrompt” is triggered. In that dialog the left panel contains prompt editing and saved‐prompt selection (using triple‑brackets delimiters), an output field dropdown (populated from the first selected note), and Start/Stop buttons. The right panel shows a table with three columns: “Progress”, “Original”, and “Generated.”
- The update process automatically writes the generated text into the chosen output field.
"""

import requests, logging, os, time, socket, sys, json
from jsonschema import validate
from anki.errors import NotFoundError
from aqt.utils import showInfo, getText
from PyQt6.QtCore import QTimer, Qt, QThread, pyqtSignal, QMetaObject
from PyQt6.QtGui import QDoubleValidator, QIntValidator, QTextOption, QAction
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QGroupBox, QComboBox, QLabel,
    QLineEdit, QFormLayout, QPushButton, QTextEdit, QHBoxLayout,
    QMessageBox, QProgressDialog, QWidget, QTableWidget, QTableWidgetItem, QMenu
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
    "TEMPERATURE": 0.2,    # one common value for all providers
    "MAX_TOKENS": 200,     # one common value for all providers
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
        "TEMPERATURE": {"type": "number"},
        "MAX_TOKENS": {"type": "integer"},
        "PROMPT": {"type": "string"},
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
# Helper Functions for Prompts
# -------------------------------
def safe_show_info(message: str) -> None:
    QTimer.singleShot(0, lambda: showInfo(message))

def load_prompt_templates() -> dict:
    """
    Load prompt templates from a file using triple‑brackets ([[[ ... ]]] as delimiters).
    """
    templates_path = os.path.join(os.path.dirname(__file__), "prompt_templates.txt")
    templates = {}
    if os.path.exists(templates_path):
        with open(templates_path, "r", encoding="utf-8") as file:
            current_key = None
            current_value = []
            for line in file:
                line = line.rstrip('\n')
                if line.startswith("[[[") and line.endswith("]]]"):
                    if current_key is not None:
                        templates[current_key] = "\n".join(current_value)
                    current_key = line[3:-3].strip()
                    current_value = []
                else:
                    current_value.append(line)
            if current_key is not None:
                templates[current_key] = "\n".join(current_value)
    return templates

def save_prompt_templates(templates: dict) -> None:
    """
    Save prompt templates to a file using triple‑brackets ([[[ ... ]]] as delimiters).
    """
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
    progress_update = pyqtSignal(int)
    note_result = pyqtSignal(object, str)
    error_occurred = pyqtSignal(object, str)
    finished_processing = pyqtSignal(int, int, int)
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
            try:
                explanation = self.generate_ai_response_callback(prompt)
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
# Main Add‑on Class
# -------------------------------
class GPTGrammarExplainer:
    @property
    def addon_dir(self) -> str:
        return os.path.dirname(__file__)
    def __init__(self):
        self.logger = logging.getLogger("OmniPromptAnki")
        self.config = self.load_config()
        # Register the settings action so the Settings dialog is accessible via the add‑on manager.
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
            "max_tokens": self.config["MAX_TOKENS"],
            "temperature": self.config["TEMPERATURE"]
        }
        return self.send_request(url, headers, data)
    def make_deepseek_request(self, prompt: str) -> str:
        url = "https://api.deepseek.com/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config['DEEPSEEK_API_KEY']}"
        }
        stream_flag = self.config.get("DEEPSEEK_STREAM", False)
        data = {
            "model": self.config["DEEPSEEK_MODEL"],
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            "temperature": self.config.get("TEMPERATURE", 0.2),
            "max_tokens": self.config.get("MAX_TOKENS", 200),
            "stream": stream_flag
        }
        timeout = 60
        try:
            response = requests.post(url, headers=headers, json=data, timeout=timeout, stream=stream_flag)
            response.raise_for_status()
        except Exception as e:
            logger.exception("DeepSeek API request failed:")
            return "[Error: API request failed]"
        if stream_flag:
            final_message = ""
            try:
                for line in response.iter_lines():
                    if line:
                        try:
                            json_line = json.loads(line.decode("utf-8"))
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

# -------------------------------
# Settings Dialog (API settings and View Log)
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
        # AI Provider Selection
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
        # View Log Button
        self.view_log_button = QPushButton("View Log")
        self.view_log_button.clicked.connect(self.show_log)
        layout.addWidget(self.view_log_button)
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
        self.provider_combo.currentIndexChanged.connect(self.update_api_options)
        self.update_api_options()
    def update_api_options(self) -> None:
        provider = self.provider_combo.currentText()
        self.model_combo.clear()
        if provider == "openai":
            self.api_key_input.setPlaceholderText("Enter OpenAI API Key")
            self.model_combo.addItems(["gpt-4o-mini", "gpt-3.5-turbo", "gpt-4o"])
        elif provider == "deepseek":
            self.api_key_input.setPlaceholderText("Enter DeepSeek API Key")
            self.model_combo.addItems(["deepseek-chat", "deepseek-reasoner"])
    def load_config(self, config: dict) -> None:
        self.config = config
        self.provider_combo.setCurrentText(self.config["AI_PROVIDER"])
        self.update_api_options()
        if self.config["AI_PROVIDER"] == "openai":
            self.api_key_input.setText(self.config.get("OPENAI_API_KEY", ""))
            self.model_combo.setCurrentText(self.config.get("OPENAI_MODEL", ""))
        else:
            self.api_key_input.setText(self.config.get("DEEPSEEK_API_KEY", ""))
            self.model_combo.setCurrentText(self.config.get("DEEPSEEK_MODEL", ""))
        self.temperature_input.setText(str(self.config.get("TEMPERATURE", 0.2)))
        self.max_tokens_input.setText(str(self.config.get("MAX_TOKENS", 200)))
    def get_updated_config(self) -> dict:
        return {
            "AI_PROVIDER": self.provider_combo.currentText(),
            "OPENAI_API_KEY": self.api_key_input.text() if self.provider_combo.currentText() == "openai" else "",
            "DEEPSEEK_API_KEY": self.api_key_input.text() if self.provider_combo.currentText() == "deepseek" else "",
            "OPENAI_MODEL": self.model_combo.currentText() if self.provider_combo.currentText() == "openai" else "",
            "DEEPSEEK_MODEL": self.model_combo.currentText() if self.provider_combo.currentText() == "deepseek" else "",
            "TEMPERATURE": float(self.temperature_input.text()),
            "MAX_TOKENS": int(self.max_tokens_input.text()),
            "PROMPT": self.config.get("PROMPT", ""),
            "SELECTED_FIELDS": self.config.get("SELECTED_FIELDS", {"output_field": "Output"}),
            "_version": DEFAULT_CONFIG["_version"]
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
# Update with OmniPrompt Dialog
# -------------------------------
class UpdateOmniPromptDialog(QDialog):
    def __init__(self, notes: list, gpt_instance: GPTGrammarExplainer, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update with OmniPrompt")
        self.notes = notes  # list of note objects
        self.gpt_instance = gpt_instance
        self.worker = None
        self.setup_ui()

    def setup_ui(self):
        main_layout = QHBoxLayout(self)
        # Left panel: prompt editing and controls.
        left_panel = QVBoxLayout()
        left_panel.addWidget(QLabel("Prompt Template:"))
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setAcceptRichText(False)
        self.prompt_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.prompt_edit.setPlainText(self.gpt_instance.config.get("PROMPT", ""))
        left_panel.addWidget(self.prompt_edit)
        left_panel.addWidget(QLabel("Saved Prompts:"))
        self.prompt_combo = QComboBox()
        self.prompt_combo.setEditable(True)
        self.prompt_combo.currentTextChanged.connect(self.load_selected_prompt)
        self.load_prompts()
        left_panel.addWidget(self.prompt_combo)
        self.save_prompt_button = QPushButton("Save Current Prompt")
        self.save_prompt_button.clicked.connect(self.save_current_prompt)
        left_panel.addWidget(self.save_prompt_button)
        left_panel.addWidget(QLabel("Output Field:"))
        self.output_field_combo = QComboBox()
        # Populate output field from the note type of the first selected note.
        if self.notes:
            first_note = self.notes[0]
            model = mw.col.models.get(first_note.mid)
            if model:
                fields = mw.col.models.field_names(model)
                self.output_field_combo.addItems(fields)
        left_panel.addWidget(self.output_field_combo)
        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self.start_processing)
        left_panel.addWidget(self.start_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_processing)
        self.stop_button.setEnabled(False)
        left_panel.addWidget(self.stop_button)
        # New "Save Changes" button to update notes with manual edits.
        self.save_changes_button = QPushButton("Save Manual Edits")
        self.save_changes_button.clicked.connect(self.save_manual_edits)
        left_panel.addWidget(self.save_changes_button)
        main_layout.addLayout(left_panel, 1)
        
        # Right panel: table with three columns: Progress, Original, and Generated.
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Progress", "Original", "Generated"])
        self.table.horizontalHeader().setStretchLastSection(True)
        # Make sure the "Generated" column is editable.
        # (By default, QTableWidgetItems are editable unless you disable editing.)
        main_layout.addWidget(self.table, 3)

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
                formatted_prompt = prompt_template.format(**note)
            except KeyError as e:
                safe_show_info(f"Missing field {e} in note {note.id}")
                continue
            note_prompts.append((note, formatted_prompt))
        if not note_prompts:
            safe_show_info("No valid notes to process.")
            return
        # Populate table: one row per note.
        self.table.setRowCount(len(note_prompts))
        for row, (note, prompt) in enumerate(note_prompts):
            progress_item = QTableWidgetItem("0%")
            try:
                original_text = note[self.output_field_combo.currentText()]
            except Exception:
                original_text = ""
            original_item = QTableWidgetItem(original_text)
            original_item.setData(Qt.ItemDataRole.UserRole, note.id)
            generated_item = QTableWidgetItem("")  # Editable by default.
            self.table.setItem(row, 0, progress_item)
            self.table.setItem(row, 1, original_item)
            self.table.setItem(row, 2, generated_item)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.worker = NoteProcessingWorker(note_prompts, self.gpt_instance.generate_ai_response)
        self.worker.note_result.connect(self.update_note_result, Qt.ConnectionType.QueuedConnection)
        self.worker.finished_processing.connect(self.processing_finished, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

    def stop_processing(self):
        if self.worker:
            self.worker.cancel()
            self.stop_button.setEnabled(False)

    def update_note_result(self, note, explanation: str):
        output_field = self.output_field_combo.currentText().strip()
        for row in range(self.table.rowCount()):
            original_item = self.table.item(row, 1)
            if original_item.data(Qt.ItemDataRole.UserRole) == note.id:
                self.table.item(row, 0).setText("100%")
                # Update the generated column with the API-generated text.
                self.table.item(row, 2).setText(explanation)
                # Automatically update the note in the collection.
                try:
                    note[output_field] = explanation
                    mw.col.update_note(note)
                except Exception as e:
                    logger.exception(f"Error updating note {note.id}: {e}")
                break

    def save_manual_edits(self):
        """Iterate over each row in the table and save the content of the 'Generated' column to the corresponding note."""
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
            "<p>Version: 1.1.1</p>"
            "<p><a href='https://ankiweb.net/shared/review/1383162606'>Rate add-on on AnkiWeb</a></p>"
            "<p>For documentation, visit:</p>"
            "<p><a href='https://github.com/stanamosov/omniprompt-anki'>GitHub Repository</a></p>"
            "<p><a href='https://codeberg.org/stanamosov/omniprompt-anki'>Codeberg Repository</a></p>"
            "<p>Credits: Stanislav Amosov</p>"
            "<p>Contact: <a href="mailto:omniprompt@mailwizard.org">omniprompt@mailwizard.org</a></p>"
        )
        label = QLabel(about_text)
        label.setOpenExternalLinks(True)
        layout.addWidget(label)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

# -------------------------------
# Tools Menu (OmniPrompt as a submenu)
# -------------------------------
def setup_omniprompt_menu():
    # Get the Tools menu from Anki.
    tools_menu = mw.form.menuTools
    omni_menu = QMenu("OmniPrompt", mw)
    settings_action = QAction("Settings", mw)
    settings_action.triggered.connect(lambda: gpt_grammar_explainer.show_settings_dialog())
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

def update_notes_with_omniprompt(note_ids: list):
    notes = [mw.col.get_note(nid) for nid in note_ids]
    dialog = UpdateOmniPromptDialog(notes, gpt_grammar_explainer, parent=mw)
    dialog.exec()

gui_hooks.browser_will_show_context_menu.append(on_browser_context_menu)

# -------------------------------
# Instantiate the Add‑on and Setup Menu
# -------------------------------
gpt_grammar_explainer = GPTGrammarExplainer()
setup_omniprompt_menu()
