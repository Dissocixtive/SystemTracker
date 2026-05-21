import tkinter as tk
import psutil
import time
import ctypes
import threading
import sys
from typing import Optional, Tuple

# --- Настройки ---
REFRESH_INTERVAL_MS = 1000  # Как часто обновлять данные (мс)
GUI_UPDATE_INTERVAL_MS = 33  # Частота обновления GUI (~30 FPS)
CPU_SAMPLE_INTERVAL = 1  # Секунд для замера CPU

# --- GPU через NVML (NVIDIA) ---
try:
    import pynvml
    pynvml.nvmlInit()
    gpu_available = True
    gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
except Exception:
    gpu_available = False

# --- Настройка прозрачного окна поверх всех ---
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
GWL_EXSTYLE = -20

def set_clickthrough(hwnd):
    try:
        ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ex_style |= WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOPMOST
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)
        ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0, 0, 0x00000001)
    except Exception:
        pass

def is_fullscreen_window(hwnd) -> bool:
    """Проверяет, находится ли окно в полноэкранном режиме."""
    try:
        # Получаем границы окна
        rect = ctypes.windll.user32.GetWindowRect(hwnd)
        if not rect:
            return False
            
        # Создаём структуру для хранения границ
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        
        window_rect = RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(window_rect))
        window_width = window_rect.right - window_rect.left
        window_height = window_rect.bottom - window_rect.top
        
        # Получаем размеры экрана
        screen_width = ctypes.windll.user32.GetSystemMetrics(0)
        screen_height = ctypes.windll.user32.GetSystemMetrics(1)
        
        # Дополнительная проверка: окно не имеет рамки
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
        is_borderless = not (style & 0x00C00000)  # WS_CAPTION
        
        # Окно считается полноэкранным, если его размеры совпадают с экраном
        # ИЛИ оно не имеет рамки и занимает весь экран
        return (window_width >= screen_width and window_height >= screen_height) or is_borderless
    except Exception:
        return False

def get_current_refresh_rate() -> float:
    """Возвращает частоту обновления монитора (Гц)."""
    try:
        # Получаем DC для основного монитора
        hdc = ctypes.windll.user32.GetDC(0)
        if hdc:
            freq = ctypes.windll.gdi32.GetDeviceCaps(hdc, 116)  # VREFRESH
            ctypes.windll.user32.ReleaseDC(0, hdc)
            if freq > 0:
                return float(freq)
    except Exception:
        pass
    return 60.0  # Возвращаем стандартное значение при ошибке

def get_active_window_info() -> Tuple[Optional[int], Optional[str]]:
    """Возвращает HWND и PID активного окна."""
    try:
        # Получаем HWND активного окна
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if hwnd:
            # Получаем PID процесса для этого окна
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            # Получаем имя процесса
            process_handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
            if process_handle:
                exe_name = ctypes.create_unicode_buffer(260)
                ctypes.windll.psapi.GetProcessImageFileNameW(process_handle, exe_name, 260)
                ctypes.windll.kernel32.CloseHandle(process_handle)
                return hwnd, exe_name.value.split('\\')[-1]
            return hwnd, None
    except Exception:
        pass
    return None, None

# --- Основной класс оверлея ---
class Overlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("SysMon")
        self.root.geometry("240x125+10+10")
        self.root.configure(bg='black')
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.attributes('-transparentcolor', 'black')
        
        # Делаем окно "кликсквозь"
        self.root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        set_clickthrough(hwnd)
        
        # Создаём метки для вывода информации
        self.lbl_active_app = tk.Label(self.root, text="Active: Desktop", font=("Consolas", 9), 
                                       bg='black', fg='#AAAAAA')
        self.lbl_fps = tk.Label(self.root, text="FPS: 0.0", font=("Consolas", 11), 
                                bg='black', fg='white')
        self.lbl_cpu = tk.Label(self.root, text="CPU:  0.0%", font=("Consolas", 11), bg='black')
        self.lbl_gpu = tk.Label(self.root, text="GPU:  0.0%", font=("Consolas", 11), bg='black')
        self.lbl_ram = tk.Label(self.root, text="RAM:  0.0%", font=("Consolas", 11), bg='black')
        
        # Размещаем метки
        self.lbl_active_app.pack(anchor='w', padx=5, pady=(5,0))
        self.lbl_fps.pack(anchor='w', padx=5, pady=1)
        self.lbl_cpu.pack(anchor='w', padx=5, pady=1)
        self.lbl_gpu.pack(anchor='w', padx=5, pady=1)
        self.lbl_ram.pack(anchor='w', padx=5, pady=(1,5))
        
        # Переменные для данных системы
        self.cpu = 0.0
        self.ram = 0.0
        self.gpu = 0.0
        self.fps = 0.0
        self.active_app_name = "Desktop"
        self.is_fullscreen = False
        self.refresh_rate = get_current_refresh_rate()
        
        # Для FPS оверлея
        self.last_time = time.perf_counter()
        self.frame_count = 0
        
        # Запускаем потоки
        self.running = True
        self.worker_thread = threading.Thread(target=self.collect_metrics, daemon=True)
        self.worker_thread.start()
        self.window_thread = threading.Thread(target=self.monitor_active_window, daemon=True)
        self.window_thread.start()
        
        # Запускаем обновление GUI
        self.update_gui()
    
    def get_color(self, percent):
        if percent < 25:
            return "#FFFFFF"
        elif percent < 50:
            return "#FFFF00"
        elif percent < 75:
            return "#FFA500"
        else:
            return "#FF0000"
    
    def collect_metrics(self):
        """Фоновый поток для сбора метрик CPU/RAM/GPU."""
        while self.running:
            self.cpu = psutil.cpu_percent(interval=CPU_SAMPLE_INTERVAL)
            self.ram = psutil.virtual_memory().percent
            
            if gpu_available:
                try:
                    util = pynvml.nvmlDeviceGetUtilizationRates(gpu_handle)
                    self.gpu = util.gpu
                except Exception:
                    self.gpu = -1.0
            else:
                self.gpu = -1.0
    
    def monitor_active_window(self):
        """Фоновый поток для мониторинга активного окна."""
        last_hwnd = None
        last_is_fullscreen = False
        
        while self.running:
            try:
                hwnd, app_name = get_active_window_info()
                
                if hwnd and hwnd != last_hwnd:
                    last_hwnd = hwnd
                    self.is_fullscreen = is_fullscreen_window(hwnd)
                    
                    if app_name:
                        # Убираем расширение .exe для красоты
                        self.active_app_name = app_name.replace('.exe', '')
                    else:
                        self.active_app_name = "Unknown App"
                    
                    # Обновляем частоту обновления при смене монитора (если нужно)
                    self.refresh_rate = get_current_refresh_rate()
                
                elif not hwnd:
                    self.active_app_name = "Desktop"
                    self.is_fullscreen = False
                    
            except Exception:
                pass
            
            time.sleep(1)  # Проверяем раз в секунду
    
    def update_gui(self):
        # Обновление FPS для оверлея
        now = time.perf_counter()
        self.frame_count += 1
        dt = now - self.last_time
        if dt >= 0.5:
            overlay_fps = self.frame_count / dt
            self.frame_count = 0
            self.last_time = now
            
            # Выбираем, что показывать в качестве FPS
            if self.is_fullscreen:
                # Если активное окно в полноэкранном режиме — показываем частоту обновления
                self.fps = self.refresh_rate
            else:
                # Иначе — FPS оверлея
                self.fps = overlay_fps
        
        # Обновляем GUI
        # Активное приложение
        app_display = f"Active: {self.active_app_name}"
        if self.is_fullscreen:
            app_display += " (Fullscreen)"
        self.lbl_active_app.config(text=app_display)
        
        # FPS
        fps_color = self.get_color(self.fps / self.refresh_rate * 100) if self.refresh_rate > 0 else "#FFFFFF"
        self.lbl_fps.config(text=f"FPS: {self.fps:.1f}", fg=fps_color)
        
        # Остальные метрики
        self.lbl_cpu.config(text=f"CPU:  {self.cpu:5.1f}%", fg=self.get_color(self.cpu))
        self.lbl_ram.config(text=f"RAM:  {self.ram:5.1f}%", fg=self.get_color(self.ram))
        
        if self.gpu >= 0:
            self.lbl_gpu.config(text=f"GPU:  {self.gpu:5.1f}%", fg=self.get_color(self.gpu))
        else:
            self.lbl_gpu.config(text="GPU:  N/A   ", fg="#888888")
        
        # Планируем следующее обновление GUI
        self.root.after(GUI_UPDATE_INTERVAL_MS, self.update_gui)
    
    def run(self):
        self.root.mainloop()
        self.running = False

if __name__ == "__main__":
    if not hasattr(ctypes, 'windll'):
        print("Эта программа работает только на Windows.")
        sys.exit(1)
    app = Overlay()
    app.run()