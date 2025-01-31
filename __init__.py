import requests
import logging
import os
from aqt import mw
from aqt.qt import *
from aqt.browser import Browser
from anki.hooks import addHook
from aqt.utils import showInfo
from aqt.qt import QProgressDialog
from aqt.utils import getText
import time
from aqt.utils import showWarning
import socket
from PyQt6.QtCore import Qt
from logging.handlers import RotatingFileHandler

AI_PROVIDERS = ["openai", "deepseek"]

DEFAULT_CONFIG = {
    "AI_PROVIDER": "openai",  # Default provider
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
    "PROMPT": "Paste your prompt here. ",
    "SELECTED_FIELDS": {
        "output_field": "Output"
    }
}

log_file = os.path.join(mw.addonManager.addonsFolder(), "omniprompt-anki", "omnPrompt-anki.log")

log_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")  # ✅ Add `encoding="utf-8"`
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_handler.setFormatter(log_formatter)

logger = logging.getLogger("OmniPromptAnki")
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

def load_prompt_templates():
    templates_path = os.path.join(mw.addonManager.addonsFolder(), "omniprompt-anki", "prompt_templates.txt")
    templates = {}

    if os.path.exists(templates_path):
        with open(templates_path, "r", encoding="utf-8") as file:
            current_key = None
            current_value = []
            for line in file:
                line = line.strip()
                if not line or line.startswith("#"):  # Ignore empty lines & comments
                    continue
                if line.startswith("[") and line.endswith("]"):  # Section header
                    if current_key and current_value:
                        templates[current_key] = "\n".join(current_value)
                    current_key = line[1:-1]  # Remove brackets
                    current_value = []
                else:
                    current_value.append(line)

            if current_key and current_value:  # Save last entry
                templates[current_key] = "\n".join(current_value)

    return templates


def save_prompt_templates(templates):
    templates_path = os.path.join(mw.addonManager.addonsFolder(), "omniprompt-anki", "prompt_templates.txt")
    with open(templates_path, "w", encoding="utf-8") as file:
        for key, value in templates.items():
            file.write(f"[{key}]\n{value}\n\n")  # Format each prompt as a section


def check_internet():
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=5)
        return True
    except OSError:
        return False

class GPTGrammarExplainer:
    def __init__(self):
        self.config = self.load_config()
        addHook("browser.setupMenus", self.on_browser_will_show)
        mw.addonManager.setConfigAction(__name__, self.show_settings_dialog)

    def make_openai_request(self, prompt):
        """Send request to OpenAI API"""
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
            
    def make_deepseek_request(self, prompt):
        """Send request to DeepSeek API"""
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config['DEEPSEEK_API_KEY']}"
        }
        data = {
            "model": self.config["DEEPSEEK_MODEL"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.config["DEEPSEEK_MAX_TOKENS"],
            "temperature": self.config["DEEPSEEK_TEMPERATURE"]
        }

        return self.send_request(url, headers, data)

    def send_request(self, url, headers, data):
        """Generalized API request function with retry mechanism"""
        retries = 3
        backoff_factor = 2
        timeout = 20

        if not check_internet():
            logger.error("No internet connection.")
            showWarning("No internet connection. Please check your network and try again.")
            return "[Error: No internet]"

        for attempt in range(retries):
            try:
                safe_data = data.copy()
                safe_data["Authorization"] = "[REDACTED]"
                logger.info(f"Sending API request: {safe_data}")
                response = requests.post(url, headers=headers, json=data, timeout=timeout)
                response.raise_for_status()
                response_text = response.json().get("choices", [{}])[0].get("message", {}).get("content", "[Error: No response]")
                logger.info(f"API response: {response_text}")
                return response_text

            except requests.exceptions.Timeout:
                logger.warning(f"Timeout error. Retrying attempt {attempt + 1}/{retries}...")
                time.sleep(backoff_factor * (attempt + 1))
            except requests.exceptions.RequestException as e:
                logger.error(f"API error: {e}")
                showWarning(f"API error: {e}")
                return "[Error: API request failed]"

        return "[Error: API request failed after multiple attempts]"
        
    def generate_ai_response(self, prompt):
        """Determine which AI provider to use"""
        provider = self.config.get("AI_PROVIDER", "openai")

        if provider == "openai":
            return self.make_openai_request(prompt)
        
        elif provider == "deepseek":
            if not self.config.get("DEEPSEEK_MODEL"):
                logger.error("DeepSeek model is missing!")
                return "[Error: No DeepSeek model selected]"

            return self.make_deepseek_request(prompt)
        
        else:
            logger.error(f"Invalid AI provider: {provider}")
            return "[Error: Invalid AI provider]"


    def show_settings_dialog(self):
        """Open settings UI"""
        dialog = SettingsDialog(mw)
        dialog.load_config(self.config)
        if dialog.exec():
            self.config = dialog.get_updated_config()
            self.save_config()

    def load_config(self):
        """Load add-on configuration"""
        config = mw.addonManager.getConfig(__name__)
        return {**DEFAULT_CONFIG, **(config or {})}

    def save_config(self):
        """Save configuration settings"""
        mw.addonManager.writeConfig(__name__, self.config)

    def on_browser_will_show(self, browser):
        """Add menu item to browser"""
        menu = browser.form.menuEdit
        self.action = QAction("Update cards with OmniPrompt", browser)
        self.action.triggered.connect(lambda: self.update_selected_notes(browser))
        menu.addAction(self.action)

    def update_selected_notes(self, browser):
        selected_notes = browser.selectedNotes()
        note_type_id = self.config.get("note_type_id", None)  # ✅ Use .get() to avoid KeyError

        if note_type_id is None:
            showWarning("No note type selected! Please configure the note type in settings.")
            return  # Exit if no note type is set

        processed_notes = 0
        modified_fields = 0

        # Ask user to confirm or select a field
        target_field, ok = getText(
            "Enter the field name where the explanation should be saved:",
            default=self.config.get("SELECTED_FIELDS", {}).get("output_field", "Output")
        )
        if not ok:
            return  # User canceled

        self.config["SELECTED_FIELDS"]["output_field"] = target_field  # Save selected field

        # Create a progress dialog
        progress = QProgressDialog("Updating cards with OmniPrompt...", "Cancel", 0, len(selected_notes), mw)
        progress.setWindowTitle("OmniPrompt Processing")
        progress.setMinimumDuration(0)  # Show instantly
        progress.setWindowModality(Qt.WindowModality.WindowModal)

        for i, note_id in enumerate(selected_notes):
            if progress.wasCanceled():
                break  # Allow users to cancel

            progress.setValue(i)
            note = mw.col.get_note(note_id)

            # Skip if the note type doesn't match
            if note.note_type()['id'] != note_type_id:
                continue

            try:
                prompt = self.config["PROMPT"].format(**note)
                explanation = self.generate_ai_response(prompt)
                target_field = self.config["SELECTED_FIELDS"]["output_field"]

                if note[target_field] != explanation:
                    note[target_field] = explanation
                    mw.col.update_note(note)
                    modified_fields += 1

                processed_notes += 1
            except KeyError as e:
                logger.error(f"Missing field {e} in note {note.id}")

        progress.setValue(len(selected_notes))  # Finish progress

        self.show_result(processed_notes, len(selected_notes), modified_fields)

        # Create a progress dialog
        progress = QProgressDialog("Updating cards with OmniPrompt...", "Cancel", 0, len(selected_notes), mw)
        progress.setWindowTitle("OmniPrompt Processing")
        progress.setMinimumDuration(0)  # Show instantly
        progress.setWindowModality(Qt.WindowModality.WindowModal)  # ✅ Fix applied

        for i, note_id in enumerate(selected_notes):
            if progress.wasCanceled():
                break  # Allow users to cancel

            progress.setValue(i)
            note = mw.col.get_note(note_id)

            # Skip if the note type doesn't match
            if note.note_type()['id'] != note_type_id:
                continue

            try:
                prompt = self.config["PROMPT"].format(**note)
                explanation = self.make_openai_request(prompt)
                target_field = self.config["SELECTED_FIELDS"]["output_field"]

                if note[target_field] != explanation:
                    note[target_field] = explanation
                    mw.col.update_note(note)
                    modified_fields += 1

                processed_notes += 1
            except KeyError as e:
                logging.error(f"Missing field {e} in note {note.id}")

        progress.setValue(len(selected_notes))  # Finish progress
        self.show_result(processed_notes, len(selected_notes), modified_fields)

    def show_result(self, processed, total, modified_fields):
        if processed:
            QMessageBox.information(
                mw, "Complete", 
                f"Updated {processed}/{total} notes.\nModified {modified_fields} fields."
            )
        else:
            QMessageBox.warning(mw, "Error", "No notes processed. Check fields/config.")

def show_result(self, processed, total, modified_fields):
    if processed:
        QMessageBox.information(
            mw, "Complete", 
            f"Updated {processed}/{total} notes.\nModified {modified_fields} fields."
        )
    else:
        QMessageBox.warning(mw, "Error", "No notes processed. Check fields/config.")

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("OmniPrompt Configuration")
        self.setMinimumWidth(500)
        self.config = None
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        
        # --- AI Provider Selection ---
        provider_group = QGroupBox("AI Provider Selection")
        provider_layout = QVBoxLayout()
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(AI_PROVIDERS)
        provider_layout.addWidget(QLabel("Select AI Provider:"))
        provider_layout.addWidget(self.provider_combo)
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
        self.api_key_input.setPlaceholderText("Enter API key (OpenAI or DeepSeek)")
        api_layout.addRow("API Key:", self.api_key_input)
        
        self.model_combo = QComboBox()
        self.model_combo.addItems(["gpt-4o-mini", "gpt-3.5-turbo", "gpt-4o", "deepseek-reasoner"])
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

        # --- Prompt Management ---
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
        prompt_layout.addWidget(QLabel("Prompt Template:"))
        prompt_layout.addWidget(self.prompt_edit)

        prompt_group.setLayout(prompt_layout)
        layout.addWidget(prompt_group)

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

        # --- Event Listeners ---
        self.note_type_combo.currentIndexChanged.connect(self.load_fields_for_selected_note_type)
        self.save_prompt_button.clicked.connect(self.save_prompt)
        self.delete_prompt_button.clicked.connect(self.delete_prompt)
        self.provider_combo.currentIndexChanged.connect(self.update_api_options)

        self.note_type_combo.currentIndexChanged.connect(self.load_fields_for_selected_note_type)

        # Load prompts on startup
        self.load_prompts()

    def update_api_options(self):
        """Update API fields based on selected provider."""
        provider = self.provider_combo.currentText()
        self.model_combo.clear()

        if provider == "openai":
            self.api_key_input.setPlaceholderText("Enter OpenAI API Key")
            self.model_combo.addItems(["gpt-4o-mini", "gpt-3.5-turbo", "gpt-4o"])
        elif provider == "deepseek":
            self.api_key_input.setPlaceholderText("Enter DeepSeek API Key")
            self.model_combo.addItems(["deepseek-chat", "deepseek-reasoner"])

    def load_config(self, config):
        """Load saved settings into the UI"""
        self.config = config
        self.provider_combo.setCurrentText(self.config["AI_PROVIDER"])

        if self.config["AI_PROVIDER"] == "openai":
            self.api_key_input.setText(self.config.get("OPENAI_API_KEY", ""))
            self.model_combo.setCurrentText(self.config.get("OPENAI_MODEL", ""))
            self.temperature_input.setText(str(self.config.get("OPENAI_TEMPERATURE", 0.2)))
            self.max_tokens_input.setText(str(self.config.get("OPENAI_MAX_TOKENS", 200)))
        else:  # DeepSeek
            self.api_key_input.setText(self.config.get("DEEPSEEK_API_KEY", ""))
            self.model_combo.setCurrentText(self.config.get("DEEPSEEK_MODEL", ""))
            self.temperature_input.setText(str(self.config.get("DEEPSEEK_TEMPERATURE", 0.2)))
            self.max_tokens_input.setText(str(self.config.get("DEEPSEEK_MAX_TOKENS", 200)))

        self.prompt_edit.setPlainText(self.config.get("PROMPT", ""))

        # Load note types
        self.note_type_combo.clear()
        for model in mw.col.models.all():
            self.note_type_combo.addItem(model['name'], userData=model['id'])

        # Set selected note type
        current_id = self.config.get("note_type_id")
        if current_id:
            index = self.note_type_combo.findData(current_id)
            if index >= 0:
                self.note_type_combo.setCurrentIndex(index)

        # Load available fields for the selected note type
        self.load_fields_for_selected_note_type()

        # Set previously selected field
        current_output = self.config["SELECTED_FIELDS"].get("output_field", "")
        if current_output:
            self.explanation_field_combo.setCurrentText(current_output)

    def load_fields_for_selected_note_type(self):
        """Load available fields for the selected note type."""
        model_id = self.note_type_combo.currentData()
        if model_id:
            note_type = mw.col.models.get(model_id)
            if note_type:
                fields = mw.col.models.field_names(note_type)
                self.explanation_field_combo.clear()
                self.explanation_field_combo.addItems(fields)

                # Set previously selected field
                current_output = self.config["SELECTED_FIELDS"].get("output_field", "")
                if current_output in fields:
                    self.explanation_field_combo.setCurrentText(current_output)

    def load_prompts(self):
        """Load saved prompts from TXT file."""
        self.prompt_combo.clear()
        prompts = load_prompt_templates()
        for name in prompts.keys():
            self.prompt_combo.addItem(name)

    def save_prompt(self):
        """Save current prompt as a template."""
        name, ok = getText("Enter a name for the prompt:")
        if ok and name:
            prompts = load_prompt_templates()
            prompts[name] = self.prompt_edit.toPlainText()
            save_prompt_templates(prompts)
            self.load_prompts()

    def delete_prompt(self):
        """Delete selected prompt."""
        name = self.prompt_combo.currentText()
        prompts = load_prompt_templates()
        if name in prompts:
            del prompts[name]
            save_prompt_templates(prompts)
            self.load_prompts()

    def get_updated_config(self):
        """Return updated settings for saving."""
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
            "note_type_id": selected_note_type_id,  # ✅ Ensure the note type ID is saved
            "PROMPT": self.prompt_edit.toPlainText(),
            "SELECTED_FIELDS": {
                "output_field": self.explanation_field_combo.currentText()  # ✅ Save output field
            }
        }


gpt_grammar_explainer = GPTGrammarExplainer()