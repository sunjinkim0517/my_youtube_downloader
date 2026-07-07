import sys
import os
import shutil
import urllib.request
import traceback
from PySide6.QtCore import Qt, QThread, Signal, Slot, QSize, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QLabel, QComboBox, QProgressBar,
    QFileDialog, QScrollArea, QFrame, QSpinBox, QCheckBox, QMessageBox
)
from PySide6.QtGui import QPixmap, QColor, QFont, QClipboard
import yt_dlp
import imageio_ffmpeg

# ------------------------------------------------------------------------
# 1. 헬퍼 함수 (Helper Functions)
# ------------------------------------------------------------------------
def format_bytes(bytes_num):
    if not bytes_num:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_num < 1024.0:
            return f"{bytes_num:.2f} {unit}"
        bytes_num /= 1024.0
    return f"{bytes_num:.2f} TB"

def format_speed(speed_bytes):
    if not speed_bytes:
        return "0 B/s"
    return f"{format_bytes(speed_bytes)}/s"

def format_eta(eta_secs):
    if not eta_secs:
        return "--:--"
    m, s = divmod(int(eta_secs), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ------------------------------------------------------------------------
# 2. 비동기 작업 스레드 (Worker Threads)
# ------------------------------------------------------------------------

class ThumbnailLoader(QThread):
    loaded = Signal(bytes)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            req = urllib.request.Request(self.url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as response:
                self.loaded.emit(response.read())
        except Exception:
            pass


class InfoExtractorWorker(QThread):
    info_extracted = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        ydl_opts = {
            'extract_flat': 'in_playlist',
            'skip_download': True,
            'nocheckcertificate': True,
            'ignoreerrors': True,
            'quiet': True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
                if info is None:
                    raise Exception("비디오 정보를 불러오지 못했습니다. URL이 올바른지 확인해주세요.")
                self.info_extracted.emit(info)
        except Exception as e:
            self.error_signal.emit(str(e))


class DownloadWorker(QThread):
    progress_signal = Signal(dict)
    finished_signal = Signal(str)
    error_signal = Signal(str)

    def __init__(self, url, options):
        super().__init__()
        self.url = url
        self.options = options
        self._is_cancelled = False

    def run(self):
        # 다운로드 진행률 및 상태를 캐치하기 위한 훅 정의
        def ydl_hook(d):
            if self._is_cancelled:
                raise Exception("cancelled_by_user")

            status_data = {
                'status': d.get('status'),
                'downloaded': d.get('downloaded_bytes', 0),
                'total': d.get('total_bytes') or d.get('total_bytes_estimate') or 0,
                'speed': d.get('speed'),
                'eta': d.get('eta'),
                'filename': d.get('filename')
            }
            self.progress_signal.emit(status_data)

        def pp_hook(d):
            if self._is_cancelled:
                raise Exception("cancelled_by_user")
            
            if d.get('status') == 'started':
                self.progress_signal.emit({
                    'status': 'processing',
                    'msg': '포맷 변환 및 오디오/비디오 병합 중...'
                })

        self.options['progress_hooks'] = [ydl_hook]
        self.options['postprocessor_hooks'] = [pp_hook]

        try:
            with yt_dlp.YoutubeDL(self.options) as ydl:
                ydl.download([self.url])
            if not self._is_cancelled:
                self.finished_signal.emit("다운로드 완료")
        except Exception as e:
            if "cancelled_by_user" in str(e) or self._is_cancelled:
                self.error_signal.emit("취소됨")
            else:
                error_msg = str(e).split('\n')[0]  # 첫 번째 줄만 표시하여 간결화
                self.error_signal.emit(f"에러: {error_msg}")

    def cancel(self):
        self._is_cancelled = True


# ------------------------------------------------------------------------
# 3. 개별 다운로드 작업 카드 위젯 (Task Widget)
# ------------------------------------------------------------------------

class TaskCard(QFrame):
    # 메인 윈도우로 시그널 전달
    cancel_requested = Signal(str)  # task_id
    delete_requested = Signal(str)  # task_id

    def __init__(self, task_id, url, title, thumbnail_url, download_format, quality, save_dir):
        super().__init__()
        self.task_id = task_id
        self.url = url
        self.title = title
        self.download_format = download_format
        self.quality = quality
        self.save_dir = save_dir
        self.status = "대기 중..."
        self.output_filepath = None

        self.setObjectName("TaskCard")
        self.init_ui(thumbnail_url)

    def init_ui(self, thumbnail_url):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(15)

        # 썸네일 이미지 라벨
        self.lbl_thumb = QLabel()
        self.lbl_thumb.setFixedSize(80, 45)
        self.lbl_thumb.setStyleSheet("background-color: #2b2b2b; border-radius: 4px;")
        self.lbl_thumb.setAlignment(Qt.AlignCenter)
        self.lbl_thumb.setText("No Image")
        layout.addWidget(self.lbl_thumb)

        if thumbnail_url:
            self.thumb_loader = ThumbnailLoader(thumbnail_url)
            self.thumb_loader.loaded.connect(self.set_thumbnail)
            self.thumb_loader.start()

        # 정보 영역 (제목 + 진행바 + 진행정보)
        info_layout = QVBoxLayout()
        info_layout.setSpacing(6)

        # 제목
        self.lbl_title = QLabel(self.title)
        self.lbl_title.setStyleSheet("font-weight: bold; font-size: 13px; color: #ffffff;")
        self.lbl_title.setWordWrap(False)
        info_layout.addWidget(self.lbl_title)

        # 진행률 표시 레이아웃 (바 + 퍼센트 텍스트)
        progress_layout = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        progress_layout.addWidget(self.progress_bar)

        self.lbl_percent = QLabel("0%")
        self.lbl_percent.setStyleSheet("font-size: 11px; font-weight: bold; min-width: 35px;")
        self.lbl_percent.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        progress_layout.addWidget(self.lbl_percent)
        info_layout.addLayout(progress_layout)

        # 상태 텍스트
        self.lbl_status = QLabel("대기 중...")
        self.lbl_status.setStyleSheet("font-size: 11px; color: #aaaaaa;")
        info_layout.addWidget(self.lbl_status)

        layout.addLayout(info_layout, stretch=1)

        # 조작 버튼 영역
        self.btn_action = QPushButton("취소")
        self.btn_action.setObjectName("actionButton")
        self.btn_action.setFixedSize(65, 28)
        self.btn_action.clicked.connect(self.on_action_clicked)
        layout.addWidget(self.btn_action)

        self.btn_open_folder = QPushButton("폴더 열기")
        self.btn_open_folder.setObjectName("actionButton")
        self.btn_open_folder.setFixedSize(75, 28)
        self.btn_open_folder.setVisible(False)
        self.btn_open_folder.clicked.connect(self.on_open_folder_clicked)
        layout.addWidget(self.btn_open_folder)

    @Slot(bytes)
    def set_thumbnail(self, data):
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            self.lbl_thumb.setPixmap(pixmap.scaled(self.lbl_thumb.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            self.lbl_thumb.setText("")

    def update_progress(self, data):
        status = data.get('status')
        if status == 'downloading':
            downloaded = data.get('downloaded', 0)
            total = data.get('total', 0)
            speed = data.get('speed')
            eta = data.get('eta')
            self.output_filepath = data.get('filename')

            if total > 0:
                percent = int(downloaded / total * 100)
                self.progress_bar.setValue(percent)
                self.lbl_percent.setText(f"{percent}%")
                status_str = f"다운로드 중... ({format_bytes(downloaded)} / {format_bytes(total)}) | 속도: {format_speed(speed)} | 남은 시간: {format_eta(eta)}"
            else:
                self.progress_bar.setValue(0)
                self.lbl_percent.setText("- %")
                status_str = f"다운로드 중... ({format_bytes(downloaded)}) | 속도: {format_speed(speed)}"

            self.lbl_status.setText(status_str)
            self.status = "다운로드 중"
        elif status == 'finished':
            self.lbl_status.setText("다운로드 완료! 파일 병합 중...")
        elif status == 'processing':
            self.lbl_status.setText(data.get('msg', '변환 중...'))

    def set_finished(self, msg):
        self.status = "완료"
        self.progress_bar.setValue(100)
        self.lbl_percent.setText("100%")
        self.lbl_status.setText("다운로드 완료")
        self.lbl_status.setStyleSheet("font-size: 11px; color: #4CAF50; font-weight: bold;")
        self.btn_action.setText("삭제")
        self.btn_open_folder.setVisible(True)

    def set_error(self, err_msg):
        self.status = "실패"
        self.lbl_status.setText(err_msg)
        if "취소" in err_msg:
            self.lbl_status.setStyleSheet("font-size: 11px; color: #aaaaaa;")
        else:
            self.lbl_status.setStyleSheet("font-size: 11px; color: #f44336; font-weight: bold;")
        self.btn_action.setText("삭제")

    def on_action_clicked(self):
        if self.status in ["대기 중...", "다운로드 중"]:
            self.cancel_requested.emit(self.task_id)
        else:
            self.delete_requested.emit(self.task_id)

    def on_open_folder_clicked(self):
        if self.output_filepath and os.path.exists(os.path.dirname(self.output_filepath)):
            # 윈도우 탐색기 열기 및 다운로드된 파일 선택
            norm_path = os.path.normpath(self.output_filepath)
            os.system(f'explorer /select,"{norm_path}"')
        else:
            # 폴더만 열기
            os.startfile(self.save_dir)


# ------------------------------------------------------------------------
# 4. 메인 윈도우 애플리케이션 (Main Window)
# ------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YouTube Downloader (한글 버전)")
        self.resize(750, 600)
        self.setMinimumSize(600, 500)

        # 변수 설정
        self.save_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        self.tasks = {}           # task_id -> {worker, card, state, ...}
        self.task_queue = []      # task_id list (FIFO)
        self.active_tasks_count = 0
        self.task_counter = 0

        self.ffmpeg_installed = True
        self.ffmpeg_path = ""
        self.check_ffmpeg()

        self.init_ui()
        self.init_stylesheet()

        # 클립보드 모니터링 타이머 설정
        self.clipboard = QApplication.clipboard()
        self.clipboard.dataChanged.connect(self.on_clipboard_changed)
        self.last_clipboard_text = ""

    def check_ffmpeg(self):
        # 1. 시스템 환경변수에서 ffmpeg 검사
        if shutil.which("ffmpeg"):
            self.ffmpeg_installed = True
            return

        # 2. imageio-ffmpeg에서 라이브러리 검사
        try:
            self.ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
            if self.ffmpeg_path and os.path.exists(self.ffmpeg_path):
                self.ffmpeg_installed = True
                return
        except Exception:
            pass

        self.ffmpeg_installed = False

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        # ---------------- 헤더 영역 ----------------
        header_layout = QVBoxLayout()
        lbl_title = QLabel("한글 유튜브 다운로더")
        lbl_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #2196F3;")
        lbl_sub = QLabel("Hitomi-Downloader 스타일의 고성능 멀티스레드 유튜브 다운로더")
        lbl_sub.setStyleSheet("font-size: 11px; color: #888888;")
        header_layout.addWidget(lbl_title)
        header_layout.addWidget(lbl_sub)
        main_layout.addLayout(header_layout)

        # ---------------- URL 입력 영역 ----------------
        input_layout = QHBoxLayout()
        self.txt_url = QLineEdit()
        self.txt_url.setPlaceholderText("유튜브 동영상 또는 재생목록(Playlist) URL을 입력하세요...")
        self.txt_url.setFixedHeight(40)
        self.txt_url.setStyleSheet("font-size: 13px;")
        input_layout.addWidget(self.txt_url, stretch=1)

        self.btn_add = QPushButton("추가")
        self.btn_add.setFixedHeight(40)
        self.btn_add.setFixedWidth(80)
        self.btn_add.clicked.connect(self.on_add_clicked)
        input_layout.addWidget(self.btn_add)
        main_layout.addLayout(input_layout)

        # ---------------- 설정 영역 ----------------
        settings_frame = QFrame()
        settings_frame.setStyleSheet("background-color: #1a1a1a; border: 1px solid #2d2d2d; border-radius: 8px;")
        settings_layout = QVBoxLayout(settings_frame)
        settings_layout.setContentsMargins(15, 12, 15, 12)
        settings_layout.setSpacing(10)

        # 1행: 저장 경로
        path_layout = QHBoxLayout()
        lbl_path_title = QLabel("저장 위치:")
        lbl_path_title.setStyleSheet("font-weight: bold;")
        path_layout.addWidget(lbl_path_title)

        self.lbl_save_path = QLabel(self.save_dir)
        self.lbl_save_path.setStyleSheet("color: #aaaaaa;")
        path_layout.addWidget(self.lbl_save_path, stretch=1)

        btn_browse = QPushButton("변경...")
        btn_browse.setObjectName("actionButton")
        btn_browse.clicked.connect(self.on_browse_clicked)
        path_layout.addWidget(btn_browse)
        settings_layout.addLayout(path_layout)

        # 2행: 세부 설정
        options_layout = QHBoxLayout()
        
        # 포맷 선택
        options_layout.addWidget(QLabel("포맷:"))
        self.cmb_format = QComboBox()
        self.cmb_format.addItems(["비디오 (MP4)", "오디오 (MP3)"])
        self.cmb_format.currentTextChanged.connect(self.on_format_changed)
        options_layout.addWidget(self.cmb_format)

        # 화질 선택
        options_layout.addWidget(QLabel("화질/음질:"))
        self.cmb_quality = QComboBox()
        options_layout.addWidget(self.cmb_quality)
        self.update_quality_combobox("비디오 (MP4)")

        # 동시 다운로드 개수
        options_layout.addWidget(QLabel("동시 다운로드 제한:"))
        self.spin_concurrency = QSpinBox()
        self.spin_concurrency.setRange(1, 10)
        self.spin_concurrency.setValue(3)
        self.spin_concurrency.setFixedWidth(50)
        options_layout.addWidget(self.spin_concurrency)

        # 클립보드 감지 체크박스
        self.chk_clipboard = QCheckBox("클립보드 감지")
        self.chk_clipboard.setChecked(True)
        options_layout.addWidget(self.chk_clipboard)

        options_layout.addStretch(1)
        settings_layout.addLayout(options_layout)

        main_layout.addWidget(settings_frame)

        # FFMPEG 경고문 (미설치 시만 노출)
        if not self.ffmpeg_installed:
            lbl_warning = QLabel("⚠ 시스템에 FFmpeg가 감지되지 않았습니다. 화질 다운그레이드 혹은 오디오 추출에 실패할 수 있습니다.")
            lbl_warning.setStyleSheet("color: #ff9800; font-size: 11px; font-weight: bold;")
            main_layout.addWidget(lbl_warning)

        # ---------------- 작업 목록 영역 ----------------
        list_header_layout = QHBoxLayout()
        self.lbl_list_count = QLabel("작업 목록 (0)")
        self.lbl_list_count.setStyleSheet("font-weight: bold; font-size: 13px; color: #ffffff;")
        list_header_layout.addWidget(self.lbl_list_count)
        list_header_layout.addStretch(1)
        
        self.btn_clear_completed = QPushButton("완료 목록 정리")
        self.btn_clear_completed.setObjectName("actionButton")
        self.btn_clear_completed.clicked.connect(self.on_clear_completed_clicked)
        list_header_layout.addWidget(self.btn_clear_completed)
        main_layout.addLayout(list_header_layout)

        # 스크롤 가능한 작업 리스트
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_widget = QWidget()
        self.scroll_widget.setStyleSheet("background-color: #121212;")
        self.task_list_layout = QVBoxLayout(self.scroll_widget)
        self.task_list_layout.setContentsMargins(0, 0, 0, 0)
        self.task_list_layout.setSpacing(8)
        self.task_list_layout.addStretch(1)  # 하단 밀어올리기용
        self.scroll_area.setWidget(self.scroll_widget)
        main_layout.addWidget(self.scroll_area, stretch=1)

        # ---------------- 하단 상태 바 ----------------
        self.lbl_footer_status = QLabel("준비 완료")
        self.lbl_footer_status.setStyleSheet("font-size: 11px; color: #888888; padding: 2px;")
        main_layout.addWidget(self.lbl_footer_status)

    def init_stylesheet(self):
        qss = """
        QMainWindow {
            background-color: #121212;
        }
        QWidget {
            color: #e0e0e0;
            font-family: 'Malgun Gothic', 'Segoe UI', Arial, sans-serif;
            font-size: 13px;
        }
        QLineEdit {
            background-color: #1e1e1e;
            border: 1px solid #2d2d2d;
            border-radius: 6px;
            padding: 8px 12px;
            color: #ffffff;
        }
        QLineEdit:focus {
            border: 1px solid #2196F3;
        }
        QComboBox {
            background-color: #1e1e1e;
            border: 1px solid #2d2d2d;
            border-radius: 6px;
            padding: 6px 12px;
            color: #ffffff;
        }
        QComboBox::drop-down {
            border: 0px;
        }
        QComboBox QAbstractItemView {
            background-color: #1e1e1e;
            border: 1px solid #2d2d2d;
            selection-background-color: #2196F3;
            selection-color: #ffffff;
        }
        QSpinBox {
            background-color: #1e1e1e;
            border: 1px solid #2d2d2d;
            border-radius: 6px;
            padding: 5px;
            color: #ffffff;
        }
        QCheckBox {
            color: #aaaaaa;
        }
        QCheckBox::indicator {
            width: 14px;
            height: 14px;
        }
        QPushButton {
            background-color: #2196F3;
            color: #ffffff;
            border: none;
            border-radius: 6px;
            padding: 8px 16px;
            font-weight: bold;
        }
        QPushButton:hover {
            background-color: #1976D2;
        }
        QPushButton:pressed {
            background-color: #0D47A1;
        }
        QPushButton#actionButton {
            background-color: #222222;
            border: 1px solid #333333;
            color: #dddddd;
            border-radius: 4px;
            font-weight: normal;
        }
        QPushButton#actionButton:hover {
            background-color: #333333;
            border: 1px solid #444444;
        }
        QScrollArea {
            border: 1px solid #2d2d2d;
            border-radius: 8px;
            background-color: #121212;
        }
        QFrame#TaskCard {
            background-color: #1a1a1a;
            border: 1px solid #2d2d2d;
            border-radius: 8px;
        }
        QFrame#TaskCard:hover {
            border: 1px solid #333333;
            background-color: #202020;
        }
        QProgressBar {
            background-color: #2b2b2b;
            border: none;
            border-radius: 3px;
            text-align: right;
            color: transparent;
            height: 6px;
        }
        QProgressBar::chunk {
            background-color: #2196F3;
            border-radius: 3px;
        }
        """
        self.setStyleSheet(qss)

    # ---------------- 콤보박스 변경 이벤트 ----------------
    def on_format_changed(self, text):
        self.update_quality_combobox(text)

    def update_quality_combobox(self, format_text):
        self.cmb_quality.clear()
        if format_text == "비디오 (MP4)":
            self.cmb_quality.addItems([
                "최고 화질 (Best)",
                "1080p",
                "720p",
                "480p",
                "360p"
            ])
        else:  # 오디오
            self.cmb_quality.addItems([
                "최고 음질 (320kbps)",
                "고음질 (256kbps)",
                "표준 음질 (192kbps)",
                "저음질 (128kbps)"
            ])

    # ---------------- 경로 찾기 ----------------
    def on_browse_clicked(self):
        selected_dir = QFileDialog.getExistingDirectory(self, "저장 경로 선택", self.save_dir)
        if selected_dir:
            self.save_dir = selected_dir
            self.lbl_save_path.setText(selected_dir)

    # ---------------- 클립보드 모니터링 ----------------
    def on_clipboard_changed(self):
        if not self.chk_clipboard.isChecked():
            return

        text = self.clipboard.text().strip()
        if not text or text == self.last_clipboard_text:
            return

        # 유튜브 URL 정규식 검증
        if "youtube.com/" in text or "youtu.be/" in text:
            self.last_clipboard_text = text
            self.txt_url.setText(text)
            self.lbl_footer_status.setText("클립보드에서 유튜브 URL을 자동으로 감지해 붙여넣었습니다.")

    # ---------------- 다운로드 추가 ----------------
    def on_add_clicked(self):
        url = self.txt_url.text().strip()
        if not url:
            QMessageBox.warning(self, "알림", "유튜브 URL을 입력해 주세요.")
            return

        # UI 비활성화
        self.btn_add.setEnabled(False)
        self.btn_add.setText("분석 중...")
        self.lbl_footer_status.setText("유튜브 URL 분석 및 정보 추출 중...")

        # 정보 분석용 비동기 스레드 실행
        self.info_worker = InfoExtractorWorker(url)
        self.info_worker.info_extracted.connect(self.on_info_extracted)
        self.info_worker.error_signal.connect(self.on_info_error)
        self.info_worker.start()

    @Slot(dict)
    def on_info_extracted(self, info):
        self.btn_add.setEnabled(True)
        self.btn_add.setText("추가")
        self.lbl_footer_status.setText("분석 완료")

        # 재생목록 여부 파악
        if info.get('_type') == 'playlist' or 'entries' in info:
            entries = info.get('entries', [])
            if not entries:
                QMessageBox.warning(self, "알림", "재생목록에 비디오가 존재하지 않습니다.")
                return

            # 전체 추가 의사 질문
            reply = QMessageBox.question(
                self, 
                "재생목록 감지", 
                f"재생목록이 감지되었습니다. 총 {len(entries)}개의 비디오를 전부 추가하시겠습니까?",
                QMessageBox.Yes | QMessageBox.No, 
                QMessageBox.Yes
            )

            if reply == QMessageBox.Yes:
                for entry in entries:
                    if entry:
                        video_url = entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}"
                        title = entry.get('title') or "제목 없음"
                        # 썸네일 URL 획득
                        thumbnails = entry.get('thumbnails', [])
                        thumbnail_url = thumbnails[-1].get('url') if thumbnails else None
                        self.add_task(video_url, title, thumbnail_url)
                self.txt_url.clear()
        else:
            title = info.get('title', '제목 없음')
            video_url = info.get('webpage_url') or self.txt_url.text().strip()
            # 썸네일 URL 획득
            thumbnails = info.get('thumbnails', [])
            thumbnail_url = info.get('thumbnail') or (thumbnails[-1].get('url') if thumbnails else None)
            self.add_task(video_url, title, thumbnail_url)
            self.txt_url.clear()

    @Slot(str)
    def on_info_error(self, err_msg):
        self.btn_add.setEnabled(True)
        self.btn_add.setText("추가")
        self.lbl_footer_status.setText("분석 실패")
        QMessageBox.critical(self, "에러", f"동영상 정보를 추출하는데 실패했습니다.\n사유: {err_msg}")

    # ---------------- 큐 및 작업 관리 ----------------
    def add_task(self, url, title, thumbnail_url):
        self.task_counter += 1
        task_id = f"task_{self.task_counter}"
        
        fmt = self.cmb_format.currentText()
        quality = self.cmb_quality.currentText()

        # 카드 위젯 생성 및 추가
        card = TaskCard(task_id, url, title, thumbnail_url, fmt, quality, self.save_dir)
        card.cancel_requested.connect(self.cancel_task)
        card.delete_requested.connect(self.delete_task)

        # Stretch 아이템 바로 위에 삽입
        self.task_list_layout.insertWidget(self.task_list_layout.count() - 1, card)

        self.tasks[task_id] = {
            'url': url,
            'title': title,
            'format': fmt,
            'quality': quality,
            'save_dir': self.save_dir,
            'card': card,
            'worker': None,
            'status': "대기 중..."
        }

        self.task_queue.append(task_id)
        self.update_list_count()
        self.process_queue()

    def process_queue(self):
        max_concurrent = self.spin_concurrency.value()
        
        while self.active_tasks_count < max_concurrent and self.task_queue:
            # 큐에서 대기 중인 다음 작업 꺼내기
            next_task_id = None
            for tid in self.task_queue:
                if self.tasks[tid]['status'] == "대기 중...":
                    next_task_id = tid
                    break

            if not next_task_id:
                break

            # 작업 시작
            self.start_task(next_task_id)

    def start_task(self, task_id):
        task = self.tasks[task_id]
        task['status'] = "다운로드 중"
        self.active_tasks_count += 1

        # yt-dlp 옵션 구성
        ydl_opts = self.build_ydl_opts(task['format'], task['quality'], task['save_dir'])

        # 워커 스레드 생성 및 바인딩
        worker = DownloadWorker(task['url'], ydl_opts)
        worker.progress_signal.connect(task['card'].update_progress)
        worker.finished_signal.connect(lambda msg, tid=task_id: self.on_task_finished(tid, msg))
        worker.error_signal.connect(lambda err, tid=task_id: self.on_task_error(tid, err))
        
        task['worker'] = worker
        worker.start()

        self.lbl_footer_status.setText(f"'{task['title']}' 다운로드를 시작했습니다.")

    def build_ydl_opts(self, fmt_text, quality_text, save_dir):
        outtmpl = os.path.join(save_dir, '%(title)s.%(ext)s')
        opts = {
            'outtmpl': outtmpl,
            'nocheckcertificate': True,
            'ignoreerrors': True,
            'logtostderr': False,
            'quiet': True,
            'no_warnings': True,
        }

        # FFmpeg 로케이션 주입
        if self.ffmpeg_installed and self.ffmpeg_path:
            opts['ffmpeg_location'] = os.path.dirname(self.ffmpeg_path)

        if fmt_text == "오디오 (MP3)":
            if self.ffmpeg_installed:
                # 음질 맵
                kbps_map = {
                    "최고 음질 (320kbps)": "320",
                    "고음질 (256kbps)": "256",
                    "표준 음질 (192kbps)": "192",
                    "저음질 (128kbps)": "128"
                }
                preferred_quality = kbps_map.get(quality_text, "192")
                opts.update({
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': preferred_quality,
                    }],
                })
            else:
                # ffmpeg가 없는 경우 변환 불가하므로 최고 오디오 파일 그대로 저장
                opts.update({
                    'format': 'bestaudio/best',
                })
        else:  # 비디오
            q_map = {
                "최고 화질 (Best)": "",
                "1080p": "[height<=1080]",
                "720p": "[height<=720]",
                "480p": "[height<=480]",
                "360p": "[height<=360]"
            }
            q_filter = q_map.get(quality_text, "")

            if self.ffmpeg_installed:
                opts.update({
                    'format': f'bestvideo{q_filter}+bestaudio/best{q_filter}',
                    'merge_output_format': 'mp4',
                })
            else:
                # ffmpeg가 없는 경우 합치지 못하므로 단일 mp4 포맷 중 최적 다운로드
                opts.update({
                    'format': f'best{q_filter}[ext=mp4]/best{q_filter}',
                })

        return opts

    @Slot(str, str)
    def on_task_finished(self, task_id, msg):
        if task_id not in self.tasks:
            return
        
        task = self.tasks[task_id]
        task['status'] = "완료"
        task['card'].set_finished(msg)

        if task_id in self.task_queue:
            self.task_queue.remove(task_id)

        self.active_tasks_count -= 1
        self.lbl_footer_status.setText(f"'{task['title']}' 다운로드가 완료되었습니다.")
        
        # 큐 재처리
        self.process_queue()

    @Slot(str, str)
    def on_task_error(self, task_id, err_msg):
        if task_id not in self.tasks:
            return

        task = self.tasks[task_id]
        task['status'] = "실패"
        task['card'].set_error(err_msg)

        if task_id in self.task_queue:
            self.task_queue.remove(task_id)

        self.active_tasks_count -= 1
        self.lbl_footer_status.setText(f"'{task['title']}' 다운로드 중 오류가 발생했습니다: {err_msg}")
        
        # 큐 재처리
        self.process_queue()

    @Slot(str)
    def cancel_task(self, task_id):
        if task_id not in self.tasks:
            return

        task = self.tasks[task_id]
        
        if task['status'] == "대기 중...":
            if task_id in self.task_queue:
                self.task_queue.remove(task_id)
            task['status'] = "실패"
            task['card'].set_error("취소됨")
            self.lbl_footer_status.setText("대기 중이던 작업을 취소했습니다.")
            self.process_queue()
        elif task['status'] == "다운로드 중":
            if task['worker']:
                # 비동기 취소 명령 전달
                task['worker'].cancel()
                self.lbl_footer_status.setText("작업을 취소하는 중...")

    @Slot(str)
    def delete_task(self, task_id):
        if task_id not in self.tasks:
            return

        task = self.tasks[task_id]
        # 카드 위젯을 레이아웃에서 제거 후 파괴
        self.task_list_layout.removeWidget(task['card'])
        task['card'].deleteLater()

        # 데이터베이스에서 작업 제거
        del self.tasks[task_id]
        
        if task_id in self.task_queue:
            self.task_queue.remove(task_id)

        self.update_list_count()
        self.lbl_footer_status.setText("목록에서 작업을 삭제했습니다.")

    def update_list_count(self):
        self.lbl_list_count.setText(f"작업 목록 ({len(self.tasks)})")

    def on_clear_completed_clicked(self):
        # 완료되었거나 실패/취소된 목록을 수집하여 삭제
        to_delete = [
            tid for tid, t in self.tasks.items() 
            if t['status'] in ["완료", "실패", "취소됨"]
        ]
        
        for tid in to_delete:
            self.delete_task(tid)

# ------------------------------------------------------------------------
# 5. 애플리케이션 진입점 (Entry Point)
# ------------------------------------------------------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # 폰트 깨짐 예방 및 깔끔한 한국어 폰트 기본 매핑
    font = QFont("Malgun Gothic", 9)
    app.setFont(font)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())
