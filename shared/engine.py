"""Mesin sensor fusion FocusNest: menggabungkan data HP (edge1) dan laptop (edge2)."""
import time  # cap waktu untuk alert
from shared import config  # ambang batas dan cooldown
from shared import processing as p  # fungsi skor


class FocusEngine:
    """Menghitung skor fokus, status, alert, dan statistik sesi belajar."""

    def __init__(self):
        self.active = False  # apakah sesi belajar sedang berjalan
        self.session_start = 0.0  # waktu mulai sesi
        self.focus_s = 0.0  # total detik dengan skor >= 70
        self.distractions = 0  # jumlah gangguan (alert) pada sesi
        self.noise_base = None  # baseline suara ruangan (dBFS)
        self.stats = p.EwmaStats()  # statistik untuk deteksi lonjakan suara
        self.last_alert = {}  # waktu alert terakhir per jenis
        self.move_streak = 0  # berapa jendela berturut-turut HP bergerak
        self.last_stamp = None  # penanda telemetri HP terakhir
        self.last_t = None  # waktu update terakhir

    def start_session(self, now):
        """Mulai sesi baru: reset statistik dan kalibrasi ulang suara ruangan."""
        self.active = True  # sesi aktif
        self.session_start = now  # catat waktu mulai
        self.focus_s = 0.0  # reset menit fokus
        self.distractions = 0  # reset gangguan
        self.noise_base = None  # kalibrasi ulang saat ruangan tenang
        self.stats = p.EwmaStats()  # reset statistik suara
        self.last_alert = {}  # reset cooldown

    def stop_session(self):
        """Akhiri sesi (statistik terakhir tetap tampil di dashboard)."""
        self.active = False  # sesi berhenti

    def _status(self, state, reason, score, scores, now):
        """Susun dict status yang dikirim ke dashboard dan HP."""
        elapsed = now - self.session_start if self.active else 0.0  # lama sesi
        return {
            "state": state,  # IDLE / WAITING / FOCUS / FAIR / DISTRACTED / PHONE_DISTRACTION / ...
            "reason": reason,  # penjelasan singkat
            "focus_score": None if score is None else round(score, 1),  # skor 0-100
            "scores": {k: None if v is None else round(v, 1) for k, v in scores.items()},  # skor per komponen
            "session": {
                "active": self.active,  # sesi berjalan?
                "elapsed_s": round(elapsed),  # lama sesi (detik)
                "focus_s": round(self.focus_s),  # detik fokus
                "distractions": self.distractions,  # jumlah gangguan
            },
            "sent_at_ms": int(time.time() * 1000),  # cap waktu
        }

    def update(self, hp, pc, now):
        """hp/pc = telemetri terbaru edge1/edge2 (None bila basi). Return (status, daftar_alert)."""
        dt = 0.0 if self.last_t is None else min(now - self.last_t, 5.0)  # selang waktu sejak update lalu
        self.last_t = now  # simpan waktu
        alerts = []  # alert yang akan dikirim balik ke HP
        if hp is None or pc is None:
            return self._status("WAITING", "Menunggu data HP (edge1) dan laptop (edge2)", None, {}, now), alerts

        # --- suara: bandingkan dengan baseline ruangan ---
        level = pc["noise_dbfs"]  # level suara dari laptop
        if self.noise_base is None:
            self.noise_base = level  # data pertama = baseline
        delta = level - self.noise_base  # kenaikan suara (dB)
        z = self.stats.zscore(delta)  # z-score lonjakan suara
        if delta < 6.0:
            self.noise_base += 0.05 * delta  # baseline ikut bergeser pelan bila ruangan berubah sedikit

        # --- HP: hitung berapa jendela berturut-turut HP bergerak ---
        moving = hp["motion_rms"] >= config.MOVE_RMS_THRESHOLD  # HP bergerak?
        stamp = hp.get("sent_at_ms")  # penanda paket telemetri
        if stamp != self.last_stamp:
            self.last_stamp = stamp  # hanya hitung sekali per paket baru
            self.move_streak = self.move_streak + 1 if moving else 0  # naik bila terus bergerak

        # --- laptop: sepi atau aktif ---
        idle = pc["keys_per_min"] < 5 and pc["mouse_moves"] < 5  # tidak mengetik dan mouse diam
        lux = None if hp.get("orientation") == "face_down" else hp.get("lux")  # sensor cahaya tertutup bila telungkup

        # --- skor tiap komponen lalu digabung (sensor fusion) ---
        scores = {
            "noise": p.noise_score(delta),  # skor suara
            "light": None if lux is None else p.light_score(lux),  # skor cahaya (None = tidak valid)
            "phone": p.phone_score(hp["motion_rms"], hp.get("orientation"), config.MOVE_RMS_THRESHOLD),  # skor HP
            "activity": p.activity_score(pc["keys_per_min"], pc["mouse_moves"]),  # skor aktivitas laptop
        }
        score = p.focus_score(scores)  # skor fokus 0-100

        # --- aturan keputusan (prioritas dari atas) ---
        event = None  # jenis gangguan yang terdeteksi
        if self.move_streak >= 2 and idle:
            event = ("PHONE_DISTRACTION", "HP terangkat saat laptop sepi. Letakkan HP, kembali fokus!")
        elif z > 3.0 and delta >= 10.0:
            event = ("NOISE_SPIKE", f"Tiba-tiba berisik (+{delta:.0f} dB dari suasana tadi).")
        elif lux is not None and lux < 100:
            event = ("TOO_DARK", f"Ruangan terlalu gelap ({lux:.0f} lux). Nyalakan lampu.")

        if event:
            state, reason = event  # gangguan jelas mengalahkan skor biasa
        else:
            state = "FOCUS" if score >= 70 else "FAIR" if score >= 40 else "DISTRACTED"  # status dari skor
            reason = f"Skor fokus {score:.0f} dari 100"
        if not self.active:
            state, reason = "IDLE", "Sesi belum dimulai. Tekan Mulai sesi di dashboard."  # hanya memantau

        # --- alert dan statistik hanya saat sesi aktif ---
        if self.active:
            if score >= 70:
                self.focus_s += dt  # tambah detik fokus
            if event and now - self.last_alert.get(event[0], -1e9) >= config.ALERT_COOLDOWN_SEC:
                self.last_alert[event[0]] = now  # catat waktu alert
                if event[0] != "TOO_DARK":
                    self.distractions += 1  # gelap bukan gangguan perilaku
                alerts.append({"type": event[0], "message": event[1], "sent_at_ms": int(time.time() * 1000)})
        return self._status(state, reason, score, scores, now), alerts
