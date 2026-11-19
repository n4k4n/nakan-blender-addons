#!/usr/bin/env python3
"""Records the Blender window through macOS, from outside the Blender process.

Why a separate process: capturing from inside Blender means asking
Blender to redraw or re-render outside its own schedule, and that's always where
the hangs came from. None of that happens here: the system hands over the window already
finished, exactly as you see it, with UV editor, Ucupaint, shader editor,
Material Preview and whatever else is on screen.

As a bonus: if Blender closes, even by crashing, this process notices and
finishes the video properly, instead of leaving a half-written file.

The add-on talks to this program through two JSON files: a configuration file,
which the add-on writes, and a state file, which it reads to show in the interface.
"""

import argparse
import ctypes
import ctypes.util
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

SCREENCAPTURE = "/usr/sbin/screencapture"


# ---------------------------------------------------------------------------
# Finding the Blender window
# ---------------------------------------------------------------------------

class _Quartz:
    """Minimal access to CoreGraphics to list windows, using only ctypes.

    Everything stays in this process on purpose: if any of these calls go
    wrong, it's this program that goes down, not Blender.
    """

    UTF8 = 0x08000100
    LISTA_TODAS = 0          # kCGWindowListOptionAll

    def __init__(self):
        self.cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        self.cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
        assinaturas = (
            ("CFStringCreateWithCString", ctypes.c_void_p,
             [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]),
            ("CFArrayGetCount", ctypes.c_long, [ctypes.c_void_p]),
            ("CFArrayGetValueAtIndex", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_long]),
            ("CFDictionaryGetValue", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]),
            ("CFNumberGetValue", ctypes.c_bool,
             [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]),
            ("CFRelease", None, [ctypes.c_void_p]),
        )
        for nome, retorno, args in assinaturas:
            f = getattr(self.cf, nome)
            f.restype = retorno
            f.argtypes = args
        self.cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
        self.cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        self._chaves = {}

    def _chave(self, texto):
        if texto not in self._chaves:
            self._chaves[texto] = self.cf.CFStringCreateWithCString(
                None, texto.encode(), self.UTF8)
        return self._chaves[texto]

    def _inteiro(self, d, chave):
        v = self.cf.CFDictionaryGetValue(d, self._chave(chave))
        if not v:
            return None
        saida = ctypes.c_int32(0)
        self.cf.CFNumberGetValue(v, 3, ctypes.byref(saida))   # kCFNumberSInt32Type
        return saida.value

    def _real(self, d, chave):
        v = self.cf.CFDictionaryGetValue(d, self._chave(chave))
        if not v:
            return None
        saida = ctypes.c_double(0)
        self.cf.CFNumberGetValue(v, 13, ctypes.byref(saida))  # kCFNumberDoubleType
        return saida.value

    def janela_principal(self, pid):
        """The largest normal window of that process: (id, width, height) in points.

        The width comes in points, not pixels. On a retina screen macOS
        hands over the shot with twice the pixels, and by comparing the two I
        find out the density without having to ask anyone.
        """
        lista = self.cg.CGWindowListCopyWindowInfo(self.LISTA_TODAS, 0)
        if not lista:
            return None
        try:
            melhor = None
            for i in range(self.cf.CFArrayGetCount(lista)):
                d = self.cf.CFArrayGetValueAtIndex(lista, i)
                if self._inteiro(d, "kCGWindowOwnerPID") != pid:
                    continue
                if self._inteiro(d, "kCGWindowLayer") != 0:
                    continue          # floating system panels, menus, etc.
                caixa = self.cf.CFDictionaryGetValue(d, self._chave("kCGWindowBounds"))
                if not caixa:
                    continue
                largura = int(self._real(caixa, "Width") or 0)
                altura = int(self._real(caixa, "Height") or 0)
                # The main window is wide AND tall; Blender's loose bars
                # show up in the list as 30 px tall strips.
                if largura < 400 or altura < 300:
                    continue
                area = largura * altura
                if melhor is None or area > melhor[1]:
                    melhor = (self._inteiro(d, "kCGWindowNumber"), area, largura, altura)
            return None if melhor is None else (melhor[0], melhor[2], melhor[3])
        finally:
            self.cf.CFRelease(lista)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def capturar(janela_id, destino):
    """Take a shot of the window. Returns the PNG bytes, or None if it failed.

    -l grabs the window itself, not the patch of screen where it is: if the Finder or
    the browser are on top of it, they don't end up in the video.
    """
    try:
        p = subprocess.run(
            [SCREENCAPTURE, "-x", "-o", "-l", str(janela_id), str(destino)],
            capture_output=True, timeout=8)
    except subprocess.TimeoutExpired:
        # I've seen screencapture hang. Eight seconds and I move on: losing
        # one shot costs nothing, freezing the whole recording does.
        return None, "screencapture took too long"
    except Exception as e:
        return None, f"screencapture failed: {e}"
    if p.returncode != 0:
        return None, (p.stderr.decode(errors="replace").strip() or "screencapture refused")
    try:
        dados = Path(destino).read_bytes()
    except Exception as e:
        return None, f"could not read the shot: {e}"
    return (dados, None) if dados else (None, "empty shot")


def tamanho_png(dados):
    """Width and height read from the PNG header, without decoding the image."""
    if len(dados) < 24 or dados[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return (int.from_bytes(dados[16:20], "big"), int.from_bytes(dados[20:24], "big"))


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------

def achar_ffmpeg(indicado):
    if indicado:
        caminho = Path(os.path.expanduser(indicado))
        if caminho.exists():
            return str(caminho)
    for c in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if Path(c).exists():
            return c
    from shutil import which
    return which("ffmpeg")


def tamanho_saida(cfg, largura, altura, pontos=None):
    if cfg.get("resolution_mode") == "FIXED":
        w, h = int(cfg.get("width", 1920)), int(cfg.get("height", 1080))
    else:
        teto = int(cfg.get("max_width", 3840))
        # Retina screen: the shot comes with twice the pixels the interface
        # measures. Recording at the size you see gives the same video at
        # half the size, which was the plan from the start.
        if pontos and pontos > 0 and largura > pontos * 1.2:
            teto = min(teto, int(pontos))
        if largura <= teto:
            w, h = largura, altura
        else:
            escala = teto / largura
            w, h = teto, max(2, int(round(altura * escala)))
    return (max(2, w - w % 2), max(2, h - h % 2))   # H.264 needs even sides


class Video:
    """A video file being written; ffmpeg eats PNGs from its input."""

    def __init__(self, cfg, largura, altura, usados, pontos=None):
        self.cfg = cfg
        self.proc = None
        self.caminho = None
        self.frames = 0
        self.origem = (largura, altura)
        self.erro = None

        ffmpeg = achar_ffmpeg(cfg.get("ffmpeg"))
        if not ffmpeg:
            self.erro = "ffmpeg not found"
            return

        agora = datetime.now()
        pasta = Path(os.path.expanduser(cfg["folder"])) / agora.strftime("%Y-%m-%d")
        try:
            pasta.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.erro = f"could not create {pasta}: {e}"
            return

        # Two videos can be born in the same second; ffmpeg only creates the file
        # later, so I reserve the name here so one doesn't eat the other.
        base = agora.strftime("%Y-%m-%d_%H-%M-%S")
        caminho = pasta / f"{base}.mp4"
        n = 2
        while str(caminho) in usados or caminho.exists():
            caminho = pasta / f"{base}_{n}.mp4"
            n += 1
        usados.add(str(caminho))
        self.caminho = caminho

        w, h = tamanho_saida(cfg, largura, altura, pontos)
        self.saida = (w, h)
        fps = int(cfg.get("fps", 30))
        filtro = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=area,"
                  f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p")
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            # Without this ffmpeg keeps "studying" the input and only creates the file
            # when the recording ends. I measured it: 8 seconds later, nothing on
            # disk. With this the header goes out on the first frame, and the video
            # exists (and plays) from the start.
            "-analyzeduration", "0", "-probesize", "32", "-fpsprobesize", "0",
            "-f", "image2pipe", "-c:v", "png", "-framerate", str(fps), "-i", "-",
            "-vf", filtro,
            # Half a frame per second arrives: plenty of CPU left to compress calmly.
            "-c:v", "libx264", "-preset", "slow", "-crf", str(int(cfg.get("crf", 23))),
            "-g", str(fps * 2),
            # Fragmented MP4: the file stays playable even after a hard shutdown.
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-flush_packets", "1",
            str(caminho),
        ]
        self.log = caminho.with_suffix(".log")
        try:
            self._log_f = open(self.log, "wb")
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL, stderr=self._log_f)
        except Exception as e:
            self.erro = f"could not start ffmpeg: {e}"

        self.min_frames = 0
        try:
            minutos = float(cfg.get("min_use_minutes", 0))
            intervalo = float(cfg.get("interval", 2))
            if minutos > 0 and intervalo > 0:
                self.min_frames = int(minutos * 60 / intervalo)
        except Exception:
            pass

    def escrever(self, png):
        if self.proc is None:
            return False
        try:
            self.proc.stdin.write(png)
            self.frames += 1
            return True
        except Exception as e:
            self.erro = f"ffmpeg stopped: {e}"
            self.proc = None
            return False

    def fechar(self):
        """Finish the file. A video that's too short is deleted instead of becoming junk."""
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=120)
            except Exception:
                self.proc.kill()
            self.proc = None
        try:
            self._log_f.close()
        except Exception:
            pass
        try:
            if self.log.exists() and self.log.stat().st_size == 0:
                self.log.unlink()
        except Exception:
            pass
        curto = self.min_frames and self.frames < self.min_frames
        if self.caminho and self.caminho.exists():
            try:
                if curto or self.frames == 0:
                    self.caminho.unlink()
                    return "discarded"
            except Exception:
                pass
        return "saved"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def ler_json(caminho, padrao=None):
    try:
        return json.loads(Path(caminho).read_text())
    except Exception:
        return padrao            # half-written: try again on the next round


def escrever_json(caminho, dados):
    try:
        tmp = Path(str(caminho) + ".tmp")
        tmp.write_text(json.dumps(dados))
        tmp.replace(caminho)     # atomic swap: the add-on never reads a half-written file
    except Exception:
        pass


def vivo(pid):
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--status", required=True)
    ap.add_argument("--probe", action="store_true",
                    help="tries one capture and exits, so the add-on knows whether it works")
    args = ap.parse_args()

    cfg = ler_json(args.config) or {}
    pid = int(cfg.get("pid", 0))
    quartz = _Quartz()

    foto = Path(tempfile.gettempdir()) / f"sr_quadro_{os.getpid()}.png"

    if args.probe:
        # Important: the test is any small patch of the screen, NOT the Blender
        # window. Capturing the window only works when it's visible, and at the
        # moment the add-on runs the test Blender may be behind
        # another app. An 8x8 rectangle answers the only question that matters
        # here: does macOS let this process capture the screen, or not?
        try:
            p = subprocess.run([SCREENCAPTURE, "-x", "-R", "0,0,8,8", str(foto)],
                               capture_output=True, timeout=15)
            erro = p.stderr.decode(errors="replace").strip()
            ok = p.returncode == 0 and foto.exists() and foto.stat().st_size > 0
        except Exception as e:
            ok, erro = False, str(e)
        achou = quartz.janela_principal(pid) is not None
        try:
            foto.unlink()
        except Exception:
            pass
        if not ok:
            print(json.dumps({"ok": False, "permissao": True,
                              "motivo": erro or "macOS refused the capture"}))
            return 1
        if not achou:
            print(json.dumps({"ok": False, "motivo": "could not find the Blender window"}))
            return 1
        print(json.dumps({"ok": True}))
        return 0

    usados = set()
    video = None
    ultima_assinatura = None
    ultima_atividade = time.monotonic()
    ultima_captura = 0.0
    parado = False
    tamanho_pendente = None
    erros_seguidos = 0
    estado = {"status": "Starting", "video": "", "frames": 0, "segmentos": 0,
              "idle": False, "falhou": False, "erro": "", "motor": "macOS window"}

    def publicar(**mudancas):
        estado.update(mudancas)
        if video is not None:
            estado["video"] = str(video.caminho or "")
            estado["frames"] = video.frames
        escrever_json(args.status, estado)

    def encerrar(motivo):
        nonlocal video
        if video is not None:
            destino = video.fechar()
            estado["segmentos"] += 1
            video = None
            publicar(status=motivo, video="", frames=0, ultimo=destino)
        else:
            publicar(status=motivo)

    def conferir_espera(agora):
        """Close the video when nothing has happened for too long.

        Idle is idle, whatever the reason: the screen may be the same, or
        Blender may be behind the browser, minimized or on another
        desktop. In all three cases no work is happening, and that's what
        "Pause after" measures. Before, I only checked when the capture worked and
        the screen was the same, so leaving Blender and going out for the night
        left the video open until the next day.
        """
        nonlocal parado
        limite = float(cfg.get("idle_timeout", 0)) * 60
        if limite > 0 and not parado and agora - ultima_atividade > limite:
            encerrar("On standby (no activity)")
            parado = True
            publicar(idle=True)

    publicar(status="Recording")
    try:
        while True:
            cfg_novo = ler_json(args.config)
            if cfg_novo:
                cfg = cfg_novo
            if cfg.get("stop") or not vivo(pid):
                encerrar("Ended")
                break

            if not cfg.get("enabled", True):
                if video is not None:
                    encerrar("Paused")
                else:
                    publicar(status="Paused")
                time.sleep(1.0)
                continue

            intervalo = float(cfg.get("interval", 2.0))
            espera = max(intervalo, 5.0) if parado else intervalo
            agora = time.monotonic()
            if agora - ultima_captura < espera:
                time.sleep(0.25)
                continue
            ultima_captura = agora

            achada = quartz.janela_principal(pid)
            if achada is None:
                conferir_espera(agora)
                time.sleep(1.0)      # minimized or on another desktop
                continue
            janela, pontos_w, _pontos_h = achada

            png, erro = capturar(janela, foto)
            if png is None:
                # macOS refuses the window when it isn't visible: another
                # app in front, another desktop, Blender minimized.
                # That's not a bug: it's exactly when there's nothing to record.
                # So I wait, without giving up and without flooding the log.
                erros_seguidos += 1
                if erros_seguidos == 3:
                    publicar(status="Waiting for the Blender window to appear",
                             erro=erro or "")
                conferir_espera(agora)
                time.sleep(1.0)
                continue
            if erros_seguidos:
                erros_seguidos = 0
                publicar(status="Recording", erro="")

            assinatura = hashlib.md5(png).digest()
            if assinatura == ultima_assinatura:
                conferir_espera(agora)
                continue

            ultima_assinatura = assinatura
            ultima_atividade = agora
            if parado:
                parado = False
                publicar(idle=False, status="Recording")

            tamanho = tamanho_png(png)
            if tamanho is None:
                continue

            partir = bool(cfg.get("split_on_resize", True))
            if video is not None and partir and tamanho != video.origem:
                # Only switch files when the new size is confirmed on the next
                # shot: dragging the window edge doesn't create ten videos.
                if tamanho_pendente == tamanho:
                    tamanho_pendente = None
                    encerrar("Recording")
                else:
                    tamanho_pendente = tamanho
                    continue
            else:
                tamanho_pendente = None

            if video is None:
                video = Video(cfg, tamanho[0], tamanho[1], usados, pontos_w)
                if video.erro:
                    publicar(falhou=True, erro=video.erro, status="Erro")
                    video = None
                    time.sleep(5.0)
                    continue
                publicar(status="Recording")

            # With splitting off, frames of another size are not
            # discarded: ffmpeg fits each one inside the video frame,
            # with bars where the aspect ratio doesn't match. Before, I threw them away, which
            # made the recording actually stop until the window went back to its
            # original size: a silent way of recording nothing.
            if partir and tamanho != video.origem:
                continue
            if not video.escrever(png):
                publicar(falhou=True, erro=video.erro or "", status="Erro")
                encerrar("Erro")
            else:
                publicar()
    finally:
        if video is not None:
            video.fechar()
        try:
            foto.unlink()
        except Exception:
            pass
        escrever_json(args.status, {**estado, "status": "Ended", "video": "", "frames": 0})
    return 0


if __name__ == "__main__":
    sys.exit(main())
