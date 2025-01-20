#!/usr/bin/env python3

# youtube_reverse_player.py
# This script downloads a YouTube video, reverses it, and plays it back in a simple GUI.
import os
import sys
import json
import shutil
import time
import queue
import threading
import tempfile
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog

import yt_dlp
import vlc  # pip install python-vlc

CONFIG_FILENAME = "settings.json"


def load_config():
    """Load user settings from a local JSON file, if present."""
    if os.path.isfile(CONFIG_FILENAME):
        try:
            with open(CONFIG_FILENAME, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {
        "temp_folder": None,
        "keep_reversed_video": False,
    }


def save_config(cfg):
    """Save user settings to JSON."""
    with open(CONFIG_FILENAME, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def format_time(seconds: float):
    """Simple mm:ss formatting."""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}:{s:02d}"


def get_available_formats(url):
    """
    Returns a deduplicated list of (height, format_id),
    ignoring anything above 1024p, sorted ascending by height.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "listformats": False,
    }
    results = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            return []
        best_for_height = {}
        for f in info.get("formats", []):
            if f.get("vcodec") == "none":
                continue
            height = f.get("height") or 0
            if height == 0:
                continue
            if height > 1024:
                continue
            fmt_id = f.get("format_id", "")
            if height not in best_for_height:
                best_for_height[height] = fmt_id
        results = [(h, best_for_height[h]) for h in best_for_height]
        results.sort(key=lambda x: x[0])
    return results


def download_video(url, format_id, output_path, progress_callback=None):
    """
    Download the chosen format to output_path using yt_dlp.
    If progress_callback is given, call it with 0–100% during download.
    """
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                fraction = downloaded / total
                if progress_callback:
                    progress_callback(fraction * 100)
        elif d["status"] == "finished":
            if progress_callback:
                progress_callback(100)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": format_id,
        "outtmpl": output_path,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


def parse_ffmpeg_progress(stderr_queue, total_duration, update_callback):
    """
    Reads lines from ffmpeg's stderr in a separate thread
    and calls update_callback(percentage) whenever 'time=...' appears.
    """
    import queue
    while True:
        try:
            line = stderr_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if line is None:  # signifying we're done
            break
        line = line.strip()
        if "time=" in line:
            parts = line.split("time=")
            if len(parts) > 1:
                t_str = parts[1].split(" ")[0]  # e.g. "00:00:04.97"
                hhmmss = t_str.split(":")
                if len(hhmmss) == 3:
                    h = float(hhmmss[0])
                    m = float(hhmmss[1])
                    s = float(hhmmss[2])
                    current = h * 3600 + m * 60 + s
                    if total_duration > 0:
                        pct = (current / total_duration) * 100
                        update_callback(pct)


def get_video_duration(video_path):
    """
    Return duration in seconds, using ffprobe.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path
    ]
    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, universal_newlines=True)
        return float(out.strip())
    except:
        return 0.0


def two_step_reverse_and_fps(in_path, out_path, user_fps, progress_callback=None):
    """
    1) Reverse the video, zero out timestamps
       -> step1_reversed.mp4
    2) Re-encode to user_fps -> final out_path
    This approach avoids big PTS jumps that can cause
    "Timestamp conversion failed" in VLC.

    No audio (-an) each step. We also add -avoid_negative_ts make_zero,
    -fflags +genpts to ensure fresh timestamps start at 0.
    """

    step1 = os.path.splitext(out_path)[0] + "_step1.mp4"
    total_dur = get_video_duration(in_path)

    # Step 1: Reverse with setpts=PTS-STARTPTS
    cmd1 = [
        "ffmpeg", "-y",
        "-fflags", "+genpts",
        "-i", in_path,
        "-vf", "reverse,setpts=PTS-STARTPTS",
        "-avoid_negative_ts", "make_zero",
        "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "36", "-threads", "2",
        step1
    ]

    # Step 2: Convert to desired FPS
    cmd2 = [
        "ffmpeg", "-y",
        "-fflags", "+genpts",
        "-i", step1,
        "-vf", f"fps={user_fps},setpts=PTS-STARTPTS",
        "-avoid_negative_ts", "make_zero",
        "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "36", "-threads", "2",
        out_path
    ]

    # We'll parse progress from the second step only (for simplicity)
    # or we can parse from both steps if we want more detailed feedback.
    def run_cmd(cmd, dur):
        stderr_lines = queue.Queue()

        def runner():
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True)
            while True:
                line = proc.stderr.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    stderr_lines.put(line)
            stderr_lines.put(None)

        t = threading.Thread(target=runner, daemon=True)
        t.start()

        # parse ffmpeg progress
        parse_ffmpeg_progress(stderr_lines, dur,
                              lambda p: progress_callback(p) if progress_callback else None)

    # Step 1
    run_cmd(cmd1, total_dur)
    # Wait a moment or just parse fully
    # In a GUI context, you'd do step 1 in a thread, wait, then step 2.

    # But for a quick synchronous approach, do:
    # We'll just run step 1 fully in a blocking manner, then step 2 with progress.
    subprocess.run(cmd1, check=True)
    # Now step 2
    dur_step2 = get_video_duration(step1)
    if progress_callback:
        progress_callback(0)  # reset to 0 for next stage
    run_cmd(cmd2, dur_step2)
    subprocess.run(cmd2, check=True)

    # cleanup
    if os.path.isfile(step1):
        os.remove(step1)


class VLCPlayerApp:
    def __init__(self, master):
        self.master = master
        self.master.title(
            "Reversed Video Player (no audio) - Two Step Approach")
        self.master.protocol("WM_DELETE_WINDOW", self.on_close)

        self.config = load_config()
        if self.config["temp_folder"] and os.path.isdir(self.config["temp_folder"]):
            self.temp_dir = self.config["temp_folder"]
        else:
            self.temp_dir = tempfile.mkdtemp(prefix="yt_reverse_")

        self.keep_reversed_video = self.config["keep_reversed_video"]
        self.downloaded_video_path = None
        self.reversed_video_path = None

        # Dark theme
        style = ttk.Style()
        style.theme_use("clam")
        bg_color = "#2b2b2b"
        fg_color = "white"
        style.configure(".", background=bg_color, foreground=fg_color)
        style.configure("TFrame", background=bg_color, foreground=fg_color)
        style.configure("TLabel", background=bg_color, foreground=fg_color)
        style.configure("TButton", background="#3c3f41", foreground=fg_color)
        style.configure("TCheckbutton", background=bg_color,
                        foreground=fg_color)
        style.configure("TEntry", fieldbackground="#3c3f41",
                        foreground="white")
        style.configure("TScale", background=bg_color, foreground=fg_color)
        style.configure("Horizontal.TProgressbar", background="#444444")

        # Layout
        self.main_frame = ttk.Frame(self.master)
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # Row 1: URL
        top_frame = ttk.Frame(self.main_frame)
        top_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)

        ttk.Label(top_frame, text="YouTube URL:").grid(
            row=0, column=0, sticky=tk.E, padx=5, pady=5)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(
            top_frame, textvariable=self.url_var, width=50)
        self.url_entry.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)

        # Right-click
        self._url_menu = tk.Menu(self.url_entry, tearoff=0)
        self._url_menu.add_command(
            label="Cut", command=lambda: self.url_entry.event_generate("<<Cut>>"))
        self._url_menu.add_command(
            label="Copy", command=lambda: self.url_entry.event_generate("<<Copy>>"))
        self._url_menu.add_command(
            label="Paste", command=lambda: self.url_entry.event_generate("<<Paste>>"))

        def _show_url_menu(e):
            self._url_menu.tk_popup(e.x_root, e.y_root)
        self.url_entry.bind("<Button-3>", _show_url_menu)

        paste_btn = ttk.Button(top_frame, text="Paste",
                               command=self.on_paste_clipboard)
        paste_btn.grid(row=0, column=2, sticky=tk.W, padx=5, pady=5)

        refresh_btn = ttk.Button(
            top_frame, text="Refresh Info", command=self.on_refresh_info)
        refresh_btn.grid(row=0, column=3, sticky=tk.W, padx=5, pady=5)

        folder_btn = ttk.Button(
            top_frame, text="Choose Temp Folder", command=self.on_choose_temp_folder)
        folder_btn.grid(row=0, column=4, sticky=tk.W, padx=5, pady=5)

        # Row 2: resolution + reversed FPS
        mid_frame = ttk.Frame(self.main_frame)
        mid_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)

        ttk.Label(mid_frame, text="Resolution:").grid(
            row=0, column=0, sticky=tk.E, padx=5, pady=5)
        self.res_var = tk.StringVar()
        self.res_combo = ttk.Combobox(
            mid_frame, textvariable=self.res_var, state="readonly", width=18)
        self.res_combo.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)

        ttk.Label(mid_frame, text="Output FPS (reversed):").grid(
            row=0, column=2, sticky=tk.E, padx=5, pady=5)
        self.fps_var = tk.IntVar(value=10)
        self.fps_slider = ttk.Scale(
            mid_frame, from_=5, to=30, orient="horizontal", variable=self.fps_var)
        self.fps_slider.grid(row=0, column=3, sticky=tk.W, padx=5, pady=5)

        self.fps_label = ttk.Label(mid_frame, text="10 FPS")
        self.fps_label.grid(row=0, column=4, sticky=tk.W, padx=5, pady=5)

        self.fps_slider.bind("<B1-Motion>", self.on_fps_slider_move)
        self.fps_slider.bind("<ButtonRelease-1>", self.on_fps_slider_move)

        self.keep_video_var = tk.BooleanVar(value=self.keep_reversed_video)
        self.keep_video_check = ttk.Checkbutton(mid_frame, text="Do not delete reversed video",
                                                variable=self.keep_video_var,
                                                command=self.on_keep_video_changed)
        self.keep_video_check.grid(
            row=0, column=5, sticky=tk.W, padx=5, pady=5)

        # Row 3: Generate + instructions
        gen_frame = ttk.Frame(self.main_frame)
        gen_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)

        self.generate_button = ttk.Button(
            gen_frame, text="Generate Reversed Video", command=self.on_generate)
        self.generate_button.pack(side=tk.LEFT, padx=5)

        instructions_btn = ttk.Button(
            gen_frame, text="Instructions", command=self.show_instructions)
        instructions_btn.pack(side=tk.LEFT, padx=5)

        # Row 4: progress label + bar
        self.progress_label = ttk.Label(self.main_frame, text="")
        self.progress_label.pack(side=tk.TOP, padx=5)

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(self.main_frame,
                                            orient="horizontal",
                                            variable=self.progress_var,
                                            length=400,
                                            mode="determinate")
        self.progress_bar.pack(side=tk.TOP, padx=5, pady=5)

        # Row 5: the VLC panel
        self.video_frame = ttk.Frame(self.main_frame)
        self.video_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.vlc_panel = tk.Canvas(self.video_frame, bg="black")
        self.vlc_panel.pack(fill=tk.BOTH, expand=True)

        # Row 6: playback controls
        ctrl_frame = ttk.Frame(self.main_frame)
        ctrl_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=5)

        self.play_button = ttk.Button(
            ctrl_frame, text="Play", command=self.on_play)
        self.play_button.pack(side=tk.LEFT, padx=5)

        self.pause_button = ttk.Button(
            ctrl_frame, text="Pause", command=self.on_pause)
        self.pause_button.pack(side=tk.LEFT, padx=5)

        self.stop_button = ttk.Button(
            ctrl_frame, text="Stop", command=self.on_stop)
        self.stop_button.pack(side=tk.LEFT, padx=5)

        self.prev_button = ttk.Button(
            ctrl_frame, text="<< Frame", command=self.on_prev_frame)
        self.prev_button.pack(side=tk.LEFT, padx=5)

        self.next_button = ttk.Button(
            ctrl_frame, text="Frame >>", command=self.on_next_frame)
        self.next_button.pack(side=tk.LEFT, padx=5)

        # timeline slider
        self.timeline_var = tk.DoubleVar()
        self.timeline_slider = ttk.Scale(ctrl_frame, from_=0, to=100, orient="horizontal",
                                         command=self.on_timeline_scrub, variable=self.timeline_var)
        self.timeline_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        self.time_label = ttk.Label(ctrl_frame, text="0:00 / 0:00")
        self.time_label.pack(side=tk.RIGHT, padx=5)

        # Row 7: playback speed (classic tk.Scale for resolution=0.1)
        speed_frame = ttk.Frame(self.main_frame)
        speed_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=5)

        ttk.Label(speed_frame, text="Playback Speed:").pack(
            side=tk.LEFT, padx=5)
        self.speed_var = tk.DoubleVar(value=1.0)

        self.speed_slider = tk.Scale(speed_frame,
                                     from_=0.1, to=3.0,
                                     orient="horizontal",
                                     resolution=0.1,
                                     variable=self.speed_var,
                                     command=self.on_speed_change)
        self.speed_slider.config(
            bg="#2b2b2b",
            fg="white",
            troughcolor="#3c3f41",
            highlightthickness=0
        )
        self.speed_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        self.speed_label = ttk.Label(speed_frame, text="1.0x")
        self.speed_label.pack(side=tk.LEFT, padx=5)

        # VLC init
        self.vlc_instance = vlc.Instance()
        self.media_player = self.vlc_instance.media_player_new()

        self.is_fullscreen = False
        self._timer_id = None
        self._length_ms = 0

        self.vlc_panel.bind("<Configure>", self.on_resize)
        self.update_timeline()

        # menu
        menu_bar = tk.Menu(self.master)
        view_menu = tk.Menu(menu_bar, tearoff=False)
        view_menu.add_command(label="Toggle Fullscreen",
                              command=self.toggle_fullscreen)
        menu_bar.add_cascade(label="View", menu=view_menu)
        self.master.config(menu=menu_bar)

    def on_close(self):
        if self._timer_id:
            self.master.after_cancel(self._timer_id)
        self.media_player.stop()

        if not self.keep_video_var.get():
            if os.path.isdir(self.temp_dir):
                shutil.rmtree(self.temp_dir, ignore_errors=True)

        self.config["temp_folder"] = self.temp_dir
        self.config["keep_reversed_video"] = self.keep_video_var.get()
        save_config(self.config)

        self.master.destroy()

    def toggle_fullscreen(self):
        self.is_fullscreen = not self.is_fullscreen
        self.master.attributes("-fullscreen", self.is_fullscreen)

    def show_instructions(self):
        win = tk.Toplevel(self.master)
        win.title("Instructions")
        msg = (
            "We do a TWO-STEP reversal to keep timestamps sane:\n"
            "1) Reverse & zero out PTS\n"
            "2) Re-encode at your chosen FPS\n"
            "This reduces VLC timestamp errors.\n\n"
            "If you pick a lower FPS than original, the final video is shorter.\n"
            "If you pick higher, it can be longer.\n"
            "No audio track is retained.\n"
            "Use the timeline slider and the Next/Prev Frame buttons.\n"
        )
        ttk.Label(win, text=msg, wraplength=400).pack(padx=20, pady=20)

    def on_resize(self, event):
        if self.media_player:
            handle = self.vlc_panel.winfo_id()
            if sys.platform.startswith('win'):
                self.media_player.set_hwnd(handle)
            elif sys.platform.startswith('linux'):
                self.media_player.set_xwindow(handle)
            elif sys.platform.startswith('darwin'):
                self.media_player.set_nsobject(handle)

    def on_choose_temp_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.temp_dir = folder
            self.log(f"Temp folder set to: {folder}")

    def on_keep_video_changed(self):
        self.keep_reversed_video = self.keep_video_var.get()

    def on_paste_clipboard(self):
        try:
            clip = self.master.clipboard_get()
            self.url_var.set(clip)
        except:
            pass

    def on_refresh_info(self):
        url = self.url_var.get().strip()
        if not url:
            self.log("Please enter a URL first.")
            return
        self.res_combo["values"] = []
        self.res_var.set("")
        self.log("Fetching format info...")

        def worker():
            try:
                fmts = get_available_formats(url)
                if not fmts:
                    raise ValueError("No valid video formats <= 1024p found.")
                items = [f"{h}p (id={fid})" for (h, fid) in fmts]

                def update_ui():
                    self.res_combo["values"] = items
                    self.res_combo.current(len(items) - 1)
                    self.log("Format info loaded.")
                self.master.after(0, update_ui)
            except Exception as e:
                self.master.after(0, lambda: self.log(
                    f"Error loading formats: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def on_fps_slider_move(self, event=None):
        val = self.fps_var.get()
        self.fps_label.config(text=f"{val} FPS")

    def on_generate(self):
        url = self.url_var.get().strip()
        if not url:
            self.log("No URL specified.")
            return
        sel = self.res_var.get()
        if not sel:
            self.log("No resolution selected.")
            return

        try:
            fmt_id = sel.split("id=")[1].replace(")", "").strip()
        except:
            self.log("Invalid resolution info.")
            return

        self.media_player.stop()

        if not os.path.isdir(self.temp_dir):
            os.makedirs(self.temp_dir, exist_ok=True)

        self.downloaded_video_path = os.path.join(
            self.temp_dir, "original.mp4")
        self.reversed_video_path = os.path.join(
            self.temp_dir, "reversed_final.mp4")

        for f in [self.downloaded_video_path, self.reversed_video_path]:
            if os.path.isfile(f):
                os.remove(f)

        self.log("Downloading...")
        self.set_progress(0)

        def download_and_reverse():
            def dl_prog(pct):
                self.master.after(
                    0, lambda: self.set_progress(pct, "Downloading..."))

            try:
                download_video(
                    url, fmt_id, self.downloaded_video_path, dl_prog)
            except Exception as e:
                self.master.after(0, lambda: self.log(f"Download error: {e}"))
                return

            self.master.after(0, lambda: self.log(
                "Download done. Now reversing in two steps..."))
            self.master.after(0, lambda: self.set_progress(0, "Reversing..."))

            user_fps = self.fps_var.get()
            total_dur = get_video_duration(self.downloaded_video_path)

            def progress_updater(pct):
                self.master.after(
                    0, lambda: self.set_progress(pct, "Reversing..."))

            try:
                two_step_reverse_and_fps(
                    in_path=self.downloaded_video_path,
                    out_path=self.reversed_video_path,
                    user_fps=user_fps,
                    progress_callback=progress_updater
                )
            except Exception as e:
                self.master.after(0, lambda: self.log(
                    f"Reverse/fps error: {e}"))
                return

            self.master.after(0, self.on_reverse_done)

        threading.Thread(target=download_and_reverse, daemon=True).start()

    def on_reverse_done(self):
        self.set_progress(100, "Reverse done.")
        self.log("Reversed video ready. Press Play.")
        self.load_vlc_media()

    def load_vlc_media(self):
        if not os.path.isfile(self.reversed_video_path):
            self.log("No reversed file found.")
            return
        media = self.vlc_instance.media_new(self.reversed_video_path)
        self.media_player.set_media(media)
        handle = self.vlc_panel.winfo_id()
        if sys.platform.startswith('win'):
            self.media_player.set_hwnd(handle)
        elif sys.platform.startswith('linux'):
            self.media_player.set_xwindow(handle)
        elif sys.platform.startswith('darwin'):
            self.media_player.set_nsobject(handle)

        self.master.after(500, self.refresh_length_info)

    def refresh_length_info(self):
        length = self.media_player.get_length()
        if length <= 0:
            self.master.after(500, self.refresh_length_info)
        else:
            self._length_ms = length
            self.timeline_slider.configure(to=length)

    def on_play(self):
        self.media_player.play()

    def on_pause(self):
        self.media_player.pause()

    def on_stop(self):
        self.media_player.stop()
        self.time_label.config(text="0:00 / 0:00")
        self.timeline_var.set(0)

    def on_prev_frame(self):
        """
        Step backward ~1 frame. The final reversed video has user_fps frames/sec.
        We'll do 1000 / user_fps ms.
        """
        user_fps = self.fps_var.get()
        if user_fps <= 0:
            return
        cur_ms = self.media_player.get_time()
        step_ms = 1000.0 / user_fps
        new_ms = cur_ms - step_ms
        if new_ms < 0:
            new_ms = 0
        self.media_player.set_time(int(new_ms))

    def on_next_frame(self):
        user_fps = self.fps_var.get()
        if user_fps <= 0:
            return
        cur_ms = self.media_player.get_time()
        step_ms = 1000.0 / user_fps
        new_ms = cur_ms + step_ms
        self.media_player.set_time(int(new_ms))

    def on_timeline_scrub(self, val):
        ms = float(val)
        self.media_player.set_time(int(ms))

    def on_speed_change(self, val):
        speed = float(val)
        self.speed_label.config(text=f"{speed:.1f}x")
        self.media_player.set_rate(speed)

    def update_timeline(self):
        if self.media_player:
            cur_ms = self.media_player.get_time()
            length_ms = self.media_player.get_length()
            if length_ms > 0:
                self.timeline_slider.configure(to=length_ms)
                self.timeline_var.set(cur_ms)
                cur_sec = cur_ms / 1000.0
                total_sec = length_ms / 1000.0
                self.time_label.config(
                    text=f"{format_time(cur_sec)} / {format_time(total_sec)}"
                )
        self._timer_id = self.master.after(250, self.update_timeline)

    def set_progress(self, pct, msg=""):
        if pct < 0:
            pct = 0
        if pct > 100:
            pct = 100
        text = f"{msg} ({pct:.1f}%)" if msg else f"{pct:.1f}%"
        self.progress_label.config(text=text)
        self.progress_var.set(pct)
        self.master.update_idletasks()

    def log(self, text):
        self.progress_label.config(text=text)
        print(text)


def main():
    root = tk.Tk()
    app = VLCPlayerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
