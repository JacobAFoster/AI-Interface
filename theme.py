# ui/theme.py

DEFAULT_THEME = {
    "bg_main": "#1e1e1e",
    "bg_panel": "#252526",
    "bg_card": "#2d2d2d",
    "fg_main": "#d4d4d4",
    "fg_muted": "#888888",
    "accent_blue": "#0e639c",
    "accent_teal": "#4ec9b0",
    "accent_purple": "#c586c0",
    "accent_red": "#f48771",
    "border": "#333333",
}

def get_theme(config):
    """Merges custom colors from app_config with the defaults."""
    theme = DEFAULT_THEME.copy()
    custom = config.get("theme_colors", {})
    theme.update(custom)
    return theme

def apply_ttk_theme(root, style, theme):
    """Applies the theme colors to ttk widgets."""
    try:
        style.theme_use("clam")
    except Exception:
        pass
    
    root.configure(bg=theme["bg_main"])
    
    style.configure(".", background=theme["bg_main"], foreground=theme["fg_main"],
                    fieldbackground=theme["bg_panel"], bordercolor=theme["border"],
                    lightcolor=theme["border"], darkcolor=theme["border"])
    
    style.configure("TNotebook", background=theme["bg_main"], borderwidth=0)
    style.configure("TNotebook.Tab", background=theme["bg_panel"], foreground=theme["fg_main"], padding=(12, 6))
    style.map("TNotebook.Tab", background=[("selected", theme["accent_blue"])], foreground=[("selected", "white")])
    
    style.configure("TFrame", background=theme["bg_main"])
    style.configure("TLabel", background=theme["bg_main"], foreground=theme["fg_main"])
    style.configure("TButton", background=theme["bg_panel"], foreground=theme["fg_main"], borderwidth=0, padding=(8, 4))
    style.map("TButton", background=[("active", theme["border"])], foreground=[("active", "white")])
    style.configure("TEntry", fieldbackground=theme["bg_panel"], foreground=theme["fg_main"])
    style.configure("TCombobox", fieldbackground=theme["bg_panel"], foreground=theme["fg_main"])
    style.configure("red.Horizontal.TProgressbar", troughcolor=theme["bg_panel"], background=theme["accent_red"])
    style.configure("Horizontal.TProgressbar", troughcolor=theme["bg_panel"], background=theme["accent_teal"])