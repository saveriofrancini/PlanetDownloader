"""Native folder dialog isolated from the web server threads."""
if __name__ == "__main__":
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    folder = filedialog.askdirectory(title="Scegli la cartella", mustexist=False)
    root.destroy()
    print(folder)
