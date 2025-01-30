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

DEFAULT_CONFIG = {
    "API_KEY": "",
    "API_ENDPOINT": "api.openai.com",
    "OPENAI_MODEL": "gpt-4o-mini",
    "OPENAI_TEMPERATURE": 0.2,
    "OPENAI_MAX_TOKENS": 200,
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
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config['API_KEY']}"
        }
        data = {
            "model": self.config["OPENAI_MODEL"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.config["OPENAI_MAX_TOKENS"],
            "temperature": self.config["OPENAI_TEMPERATURE"]
        }

        retries = 3
        backoff_factor = 2
        timeout = 20

        if not check_internet():
            logger.error("No internet connection. Request failed.")
            showWarning("No internet connection. Please check your network and try again.")
            return "[Error: No internet]"

        for attempt in range(retries):
            try:
                logger.info(f"Sending API request: {data}")
                response = requests.post(url, headers=headers, json=data, timeout=timeout)
                response.raise_for_status()  # Check HTTP errors

                response_text = response.json().get("choices", [{}])[0].get("message", {}).get("content", "[Error: No response]")
                logger.info(f"API response: {response_text}")

                return response_text
                
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout error. Retrying attempt {attempt + 1}/{retries}...")
                if attempt < retries - 1:
                    time.sleep(backoff_factor * (attempt + 1))
                else:
                    logger.error("OpenAI API request failed after multiple attempts.")
                    showWarning("OpenAI API request timed out after multiple attempts. Try again later.")
                    return "[Error: API request timed out]"

            except requests.exceptions.RequestException as e:
                logger.error(f"OpenAI API error: {e}")
                showWarning(f"OpenAI API error: {e}")
                return "[Error: API request failed]"

        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config['API_KEY']}"
        }
        data = {
            "model": self.config["OPENAI_MODEL"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.config["OPENAI_MAX_TOKENS"],
            "temperature": self.config["OPENAI_TEMPERATURE"]
        }

        retries = 3  # Number of retry attempts
        backoff_factor = 2  # Delay multiplier (2, 4, 8 seconds)
        timeout = 20  # Increased timeout from 10 → 20 seconds

        if not check_internet():
            showWarning("No internet connection. Please check your network and try again.")
            return "[Error: No internet]"
        
        for attempt in range(retries):
            try:
                response = requests.post(url, headers=headers, json=data, timeout=timeout)
                response.raise_for_status()  # Check for HTTP errors
                return response.json().get("choices", [{}])[0].get("message", {}).get("content", "[Error: No response]")
            
            except requests.exceptions.Timeout:
                if attempt < retries - 1:
                    wait_time = backoff_factor * (attempt + 1)
                    logging.warning(f"Timeout error. Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)  # Wait before retrying
                else:
                    showWarning("OpenAI API request timed out after multiple attempts. Try again later.")
                    return "[Error: API request timed out]"

            except requests.exceptions.RequestException as e:
                showWarning(f"OpenAI API error: {e}")
                return "[Error: API request failed]"

    def show_settings_dialog(self):
        dialog = SettingsDialog(mw)
        dialog.load_config(self.config)  # Load existing config
        if dialog.exec():  # If user clicks "Save"
            self.config = dialog.get_updated_config()
            self.save_config()  # Save the updated configuration

    def load_config(self):
        config = mw.addonManager.getConfig(__name__)
        return {**DEFAULT_CONFIG, **(config or {})}

    def save_config(self):
        mw.addonManager.writeConfig(__name__, self.config)

    def on_browser_will_show(self, browser):
        menu = browser.form.menuEdit
        self.action = QAction("Update cards with OmniPrompt", browser)
        self.action.triggered.connect(lambda: self.update_selected_notes(browser))
        menu.addAction(self.action)

    def update_selected_notes(self, browser):  # ✅ Ensure this is inside the class
        selected_notes = browser.selectedNotes()
        note_type_id = self.config["note_type_id"]
        processed_notes = 0
        modified_fields = 0

        # Ask user to confirm or select a field
        target_field, ok = getText("Enter the field name where the explanation should be saved:", default=self.config["SELECTED_FIELDS"]["output_field"])
        if not ok:
            return  # User canceled

        self.config["SELECTED_FIELDS"]["output_field"] = target_field  # Save selected field

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
        self.api_key_input.setPlaceholderText("Enter your OpenAI API key")
        api_layout.addRow("API Key:", self.api_key_input)
        
        self.model_combo = QComboBox()
        self.model_combo.addItems(["gpt-4o-mini", "gpt-3.5-turbo", "gpt-4o"])
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

        # Prompt Settings
        prompt_group = QGroupBox("Prompt Template")
        self.prompt_edit = QTextEdit()
        prompt_group.setLayout(QVBoxLayout())
        prompt_group.layout().addWidget(self.prompt_edit)
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
        self.note_type_combo.currentIndexChanged.connect(self.load_fields_for_selected_note_type)

    def load_config(self, config):
        self.config = config
        self.api_key_input.setText(self.config["API_KEY"])
        self.model_combo.setCurrentText(self.config["OPENAI_MODEL"])
        self.temperature_input.setText(str(self.config["OPENAI_TEMPERATURE"]))
        self.max_tokens_input.setText(str(self.config["OPENAI_MAX_TOKENS"]))
        self.prompt_edit.setPlainText(self.config["PROMPT"])
        
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
                self.load_fields_for_selected_note_type()

    def load_fields_for_selected_note_type(self):
        model_id = self.note_type_combo.currentData()
        if model_id:
            note_type = mw.col.models.get(model_id)
            if note_type:
                fields = mw.col.models.field_names(note_type)
                self.explanation_field_combo.clear()  # ✅ Only clear the Output Field dropdown
                self.explanation_field_combo.addItems(fields)

                # Set the previously selected output field
                current_output = self.config["SELECTED_FIELDS"].get("output_field", "")
                if current_output in fields:
                    self.explanation_field_combo.setCurrentText(current_output)

    def get_updated_config(self):
        return {
            "note_type_id": self.note_type_combo.currentData(),
            "API_KEY": self.api_key_input.text(),
            "OPENAI_MODEL": self.model_combo.currentText(),
            "OPENAI_TEMPERATURE": float(self.temperature_input.text()),
            "OPENAI_MAX_TOKENS": int(self.max_tokens_input.text()),
            "PROMPT": self.prompt_edit.toPlainText(),
            "SELECTED_FIELDS": {
                "output_field": self.explanation_field_combo.currentText()
            }
        }

gpt_grammar_explainer = GPTGrammarExplainer()