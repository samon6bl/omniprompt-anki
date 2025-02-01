# OmniPrompt Anki 🚀  

**OmniPrompt Anki** is an Anki add-on that enhances your flashcards by generating AI-powered fields. It integrates with LLM models to provide **detailed grammar explanations, translations, definitions**, whatever you want to add to your deck!

## Features
✅ **AI-Powered Explanations** – Uses LLM models to generate your card field content.  
✅ **Supports OpenAI & DeepSeek** – Choose your AI provider.  
✅ **Custom Prompts** – Configure and save personalized prompt templates.  
✅ **Batch Processing** – Process multiple notes at once.  
✅ **Progress Tracking** – A progress bar shows real-time updates for batch processing.  
✅ **Field Selection** – Choose which field to update **before running**.  
✅ **Use Any Note Field in Prompt** – Dynamically insert note fields into the prompt.  

---

## ⚠️ DeepSeek API Currently Not Working ⚠️

At the moment, **DeepSeek API requests may fail or time out**, leading to incomplete or missing responses.  
This issue is not related to **OmniPrompt-Anki**, but rather an ongoing problem with DeepSeek's API services.  

**Check DeepSeek's current status here:** [DeepSeek API Status](https://status.deepseek.com/)

We recommend using **OpenAI** as the AI provider until the issue is resolved.

---

## User Interface

To help you understand how **OmniPrompt-Anki** works, here are some key interface elements.

| Feature | Description | Screenshot |
|---------|------------|------------|
| **1️⃣ Settings Menu** | Configure API Key, AI model, and prompt template. Select the note type before choosing fields. | ![Settings Menu](docs/user_interface/settings_screenshot.jpg) |
| **2️⃣ Selecting the Explanation Field** | Before updating, you must confirm which field will be overwritten. **Warning:** This action will replace existing content in the selected field. | ![Field Selection](docs/user_interface/field_selection_screenshot.jpg) |
| **3️⃣ Running OmniPrompt in the Anki Browser** | Select notes and click **"Update cards with OmniPrompt"** in the **Edit menu**. The progress bar will indicate real-time updates. | ![Anki Browser Menu](docs/user_interface/anki_browser_screenshot.jpg) |

---

## Installation

### **From AnkiWeb**
1. Open Anki and go to **Tools → Add-ons → Get Add-ons**.
2. Enter the add-on code: 
   ```
   1383162606
   ```
3. Restart Anki to complete the installation.

### **From Codeberg or GitHub**
#### **1️⃣ Clone the Repository**
```sh
git clone https://codeberg.org/stanamosov/omniprompt-anki.git
# or from Github
git clone https://github.com/stanamosov/omniprompt-anki.git
```
#### **2️⃣ Install the Add-on**
1. Navigate to your Anki add-ons directory:
   - **macOS/Linux**: `~/.local/share/Anki2/addons21/`
   - **Windows**: `%APPDATA%\Anki2\addons21\`
2. Copy the `omniprompt-anki` folder into the add-ons directory.
3. Restart Anki.

---

## Setup
1. Open Anki and go to **Tools → Add-ons → OmniPrompt-Anki → Config**.
2. Enter your **OpenAI** or **DeepSeek** **API key**.
3. Choose the **AI model** (`gpt-4o`, `gpt-3.5-turbo`, `deepseek-chat`, etc.).
4.  **Select a note type** before choosing the fields.  
   🔹 *By default, the first note type in your collection is selected.*
5. **Set the field where AI-generated explanations should be saved.**
6. **Customize your prompt template using placeholders from your note fields.** *(See examples below)*
7. Click **Save** and start using the add-on!

---

## How It Works
1. **Select notes in the Anki Browser**.
2. **Click “Update cards with OmniPrompt”** in the **Edit menu**.
3. **Confirm which field will be overwritten.**  
   🚨 **Warning:** All existing data in the selected field **will be replaced** with AI-generated content.
4. The add-on **sends a request to OpenAI or DeepSeek** with the configured prompt.
5. AI-generated content **is saved in the selected field**.
6. A **confirmation message** shows how many notes were updated.

---

## Examples of Use

### **Automated Word Translations**  
**Prompt Example:**  
```plaintext
Translate the word "{Front}" into French, provide a detailed explanation, and give example sentences with translations.
```
**Use Case:** Useful for building bilingual vocabulary decks.

### **Grammar Explanation for Cloze Sentences**  
**Prompt Example:**  
```plaintext
Analyze the grammar of this sentence: "{Cloze}". Explain the function of each word and provide alternative phrasings.
```
**Use Case:** Perfect for **Cloze Deletion decks**, where learners focus on missing words.

### **Detailed Verb Conjugations**  
**Prompt Example:**  
```plaintext
Provide full conjugation tables for the verb "{Front}" in present, past, and future tenses in Spanish.
```
**Use Case:** Helps language learners quickly **memorize verb forms**.

### **Contextual Example Generation**  
**Prompt Example:**  
```plaintext
Generate three example sentences using the word "{Front}" in different contexts. Provide explanations for each.
```
**Use Case:** Expands word usage knowledge, **reinforcing retention**.

### **Phonetic Breakdown & Pronunciation Tips**  
**Prompt Example:**  
```plaintext
Provide an IPA transcription and pronunciation tips for the word "{Front}". Explain difficult sounds for non-native speakers.
```
**Use Case:** Great for learning **pronunciation of foreign words**.

### **Synonyms, Antonyms & Related Words**  
**Prompt Example:**  
```plaintext
List 5 synonyms and 5 antonyms for the word "{Front}". Include example sentences.
```
**Use Case:** Helps expand vocabulary by learning **word relationships**.

### **Cultural Context & Usage**  
**Prompt Example:**  
```plaintext
Explain the cultural nuances of the phrase "{Front}" in Japanese. When is it appropriate or inappropriate to use it?
```
**Use Case:** Ideal for learners of **Japanese, Chinese, and other languages** with strong contextual meanings.

These are just **a few examples**, but you can **fully customize your prompts** to fit any learning style!

---

## Customizing the Prompt with Note Fields
You can use **any field from your selected note type** inside the prompt. Field names are **case sensitive**!

### **Example Prompt Using Note Fields**
  **PROMPT**: 
  ```
  Generate a detailed explanation for this word: {Front}. Include examples and a grammar breakdown.
  ```

### **Using Multiple Fields**
  **PROMPT**: 
  ```
  Generate a detailed explanation for this japanese word: {Japanese Word}. Include this example: "{Sentence}" in explanation.
  ```
  This dynamically puls **both Japanese Word and Sentence fields** into the AI request.

### **🚨 Warning: Field Overwrite**
Before running, the add-on will **ask you to confirm** the field where the AI-generated content will be saved.  
**All existing content in this field will be replaced.**

---

## Logging
This add-on maintains a log file (**omnPrompt-anki.log**) inside the add-ons folder.  
This log captures API requests, responses, and errors for debugging purposes. The log file is limited to **5MB**, and up to **two backups** are maintained to prevent excessive disk usage.

If you encounter issues, check the log file for:
  - API connection failures
  - Timeout errors
  - Invalid JSON responses

---

## 🤝 Contributing
We welcome contributions! 🛠️

### **How to Contribute**
1. **Fork the repository** on [Codeberg](https://codeberg.org/stanamosov/omniprompt-anki) or [GitHub](https://github.com/stanamosov/omniprompt-anki).
2. **Create a new branch** (`feature-new-functionality`).
3. **Make your changes** and **test in Anki**.
4. **Submit a pull request** with a clear description.

### **Ways to Help**
- **Bug reports & feature requests**: Open an issue on [Codeberg](https://codeberg.org/stanamosov/omniprompt-anki) or [GitHub](https://github.com/stanamosov/omniprompt-anki).
- **Code improvements**: Help optimize or add new features.
- **Documentation**: Improve instructions.

---
## 🛠️ Roadmap
### **✅ Completed**
- [x] Selectable note fields in the prompt  
- [x] **Customization UI** – More user-friendly settings configuration.  
- [x] **More AI Models** – Add support for DeepeSeek and other LLMs.  

### **🚀 Planned Features** 
- [ ] **LLM's** – More LLM's support.  

---

## 📜 License
This project is licensed under the [**MIT License**](docs/LICENSE).  
You are free to use, modify, and distribute the code with attribution.

---

## ❤️ Support & Feedback
- Found a bug? Open an **issue** on [Codeberg](https://codeberg.org/stanamosov/omniprompt-anki) or [GitHub](https://github.com/stanamosov/omniprompt-anki).
- Have suggestions? **We’d love to hear your feedback!**  
- Want to contribute? Check out the **Contributing** section.

Enjoy smarter studying with **OmniPrompt Anki Plugin**! 🚀

