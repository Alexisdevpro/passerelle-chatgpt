"""Native local model selector. No admin HTTP endpoint or browser-held secret."""

import asyncio
import queue
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk

from .errors import GatewayError
from .selection import DEFAULT_ALIAS, load_catalog, selectable_efforts, validate_selection

EFFORT_LABELS = {
    None: "Automatique — réglage du modèle",
    "none": "Instant — sans raisonnement (none)",
    "minimal": "Minimal (minimal)",
    "low": "Faible (low)",
    "medium": "Moyen (medium)",
    "high": "Élevé (high)",
    "xhigh": "Très élevé (xhigh)",
    "max": "Maximum (max)",
}


class Chooser:
    def __init__(self, settings, storage):
        self.settings, self.storage = settings, storage
        self.root = tk.Tk()
        self.root.title("Passerelle — Modèle et raisonnement")
        self.root.geometry("740x550")
        self.root.minsize(660, 530)
        self.root.configure(background="#f3f5fa")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10))
        style.configure("TFrame", background="#f3f5fa")
        style.configure("TLabel", background="#f3f5fa", foreground="#17243b")
        style.configure("Heading.TLabel", font=("Segoe UI", 19, "bold"))
        style.configure("Muted.TLabel", foreground="#53627a")
        style.configure("TCombobox", padding=8)
        style.configure("TButton", padding=(14, 8))
        style.configure("Accent.TButton", background="#234bc5", foreground="white")
        style.map("Accent.TButton", background=[("active", "#16369b"), ("disabled", "#a9b4d2")])
        self.models = []
        self.effort_values = [None]
        self.closed = False
        self.busy = False
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.results = queue.SimpleQueue()
        self.model_var = tk.StringVar()
        self.effort_var = tk.StringVar(value=EFFORT_LABELS[None])
        self.status_var = tk.StringVar(value="Chargement du catalogue de votre compte…")
        self.hint_var = tk.StringVar(value="")
        frame = ttk.Frame(self.root, padding=26)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="Votre modèle, votre choix", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        backend = "Abonnement ChatGPT" if settings.provider == "chatgpt_oauth" else "Ollama local"
        ttk.Label(frame, text=backend + " · Réglages appliqués sans redémarrage", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(5, 22)
        )
        ttk.Label(frame, text="Modèle").grid(row=2, column=0, sticky="w")
        self.model_combo = ttk.Combobox(frame, textvariable=self.model_var, state="disabled")
        self.model_combo.grid(row=3, column=0, sticky="ew", pady=(5, 16))
        self.model_combo.bind("<<ComboboxSelected>>", self.model_changed)
        ttk.Label(frame, text="Effort de raisonnement").grid(row=4, column=0, sticky="w")
        self.effort_combo = ttk.Combobox(frame, textvariable=self.effort_var, state="disabled")
        self.effort_combo.grid(row=5, column=0, sticky="ew", pady=(5, 5))
        ttk.Label(frame, textvariable=self.hint_var, style="Muted.TLabel", wraplength=650).grid(
            row=6, column=0, sticky="w"
        )
        ttk.Label(
            frame, text="Un effort plus élevé peut demander plus de temps et de quota.", style="Muted.TLabel"
        ).grid(row=7, column=0, sticky="w", pady=(8, 16))
        ttk.Label(
            frame, text=f"Dans vos logiciels, choisissez le modèle « {DEFAULT_ALIAS} ».", wraplength=650
        ).grid(row=8, column=0, sticky="w")
        ttk.Label(
            frame, text="Un modèle ou un effort précisé dans l'appel reste prioritaire.", style="Muted.TLabel"
        ).grid(row=9, column=0, sticky="w", pady=(3, 12))
        controls = ttk.Frame(frame)
        controls.grid(row=10, column=0, sticky="ew")
        self.save_button = ttk.Button(
            controls, text="Enregistrer", command=self.save, style="Accent.TButton", state="disabled"
        )
        self.save_button.pack(side="left")
        self.refresh_button = ttk.Button(controls, text="Actualiser", command=self.refresh)
        self.refresh_button.pack(side="left", padx=9)
        ttk.Button(controls, text="Copier le nom à utiliser", command=self.copy_alias).pack(side="left")
        ttk.Label(frame, textvariable=self.status_var, wraplength=650, style="Muted.TLabel").grid(
            row=11, column=0, sticky="ew", pady=(16, 0)
        )
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Configure>", self.resize_labels)
        self.root.after(100, self.refresh)
        self.root.after(100, self.poll)

    def resize_labels(self, _event):
        # Labels shrink with the native window and with Windows display scaling.
        for widget in self.root.winfo_children()[0].winfo_children():
            if isinstance(widget, ttk.Label) and widget.cget("wraplength"):
                widget.configure(wraplength=max(450, self.root.winfo_width() - 60))

    def selected_model(self):
        index = self.model_combo.current()
        return self.models[index] if 0 <= index < len(self.models) else None

    def selected_effort(self):
        index = self.effort_combo.current()
        return self.effort_values[index] if 0 <= index < len(self.effort_values) else None

    def model_changed(self, _event=None, preferred=None):
        model = self.selected_model()
        self.effort_values = [None, *(selectable_efforts(model) if model else [])]
        self.effort_combo["values"] = [EFFORT_LABELS[value] for value in self.effort_values]
        self.effort_combo.current(
            self.effort_values.index(preferred) if preferred in self.effort_values else 0
        )
        self.effort_combo["state"] = "readonly" if model else "disabled"
        if model and len(self.effort_values) > 1:
            self.hint_var.set("Niveaux annoncés par le catalogue de votre compte pour ce modèle.")
        else:
            self.hint_var.set(
                "Seul le réglage automatique est disponible dans cet adaptateur pour ce modèle."
            )

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        self.save_button["state"] = "disabled"
        self.refresh_button["state"] = "disabled"
        self.model_combo["state"] = "disabled"
        self.effort_combo["state"] = "disabled"
        self.status_var.set("Chargement du catalogue de votre compte…")

        def fetch():
            try:
                self.results.put((asyncio.run(load_catalog(self.settings, self.storage)), None))
            except Exception as exc:
                self.results.put(
                    (
                        None,
                        exc.message
                        if isinstance(exc, GatewayError)
                        else "Catalogue inaccessible. Vérifiez la connexion ChatGPT ou le démarrage d'Ollama.",
                    )
                )

        self.worker.submit(fetch)

    def poll(self):
        if self.closed:
            return
        try:
            models, error = self.results.get_nowait()
        except queue.Empty:
            self.root.after(100, self.poll)
            return
        self.busy = False
        self.refresh_button["state"] = "normal"
        if error or not models:
            self.status_var.set(error or "Aucun modèle visible sur ce compte.")
        else:
            previous = self.selected_model()
            preferred_effort = self.selected_effort()
            saved = self.storage.preferences(self.settings.provider)
            target = previous["id"] if previous else (saved["model"] if saved else "gpt-6-luna")
            preferred_effort = preferred_effort if previous else (saved["effort"] if saved else None)
            self.models = models
            self.model_combo["values"] = [model["id"] for model in models]
            index = next((i for i, model in enumerate(models) if model["id"] == target), 0)
            self.model_combo.current(index)
            self.model_combo["state"] = "readonly"
            self.model_changed(preferred=preferred_effort)
            self.save_button["state"] = "normal"
            self.status_var.set(
                f"{len(models)} modèles dans le catalogue. Choisissez vos réglages, puis enregistrez."
            )
        self.root.after(100, self.poll)

    def save(self):
        model = self.selected_model()
        if not model:
            return
        effort = self.selected_effort()
        try:
            validate_selection(self.models, model["id"], effort, self.settings.provider)
            self.storage.set_preferences(self.settings.provider, model["id"], effort)
        except GatewayError as exc:
            self.status_var.set(exc.message)
            return
        self.status_var.set(
            f"Enregistré : {model['id']} · {EFFORT_LABELS[effort]}. Les prochains appels à {DEFAULT_ALIAS} suivront ce choix."
        )

    def copy_alias(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(DEFAULT_ALIAS)
        self.root.update_idletasks()
        self.status_var.set(f"« {DEFAULT_ALIAS} » copié. Enregistrez votre sélection pour l'utiliser.")

    def close(self):
        self.closed = True
        self.worker.shutdown(wait=False)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def choose(settings, storage):
    Chooser(settings, storage).run()
