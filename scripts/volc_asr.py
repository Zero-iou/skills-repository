#!/usr/bin/env python3
"""
火山引擎录音文件极速识别
先录制音频到 wav，录制结束后调用 API 一次性转录
ffmpeg 输出原始 PCM 避免 WAV muxer 缓冲问题，Python 手动包 WAV header
"""

import argparse
import asyncio
import atexit
import base64
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.request
import urllib.error

APPID = ""
ACCESS_TOKEN = ""
API_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
RESOURCE_ID = "volc.bigasr.auc_turbo"

ffmpeg_procs = []
running = True
prev_audio_device = None
pid_file = None
stop_reason = "manual"
record_start_time = None


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def notify(title, text):
    safe = text.replace('"', '\\"').replace("\n", " ")[:200]
    safe_title = title.replace('"', '\\"')
    subprocess.run([
        "osascript", "-e",
        f'display notification "{safe}" with title "{safe_title}"'
    ], capture_output=True)


def get_pid_file(mode):
    return os.path.expanduser(f"~/.hammerspoon/volc_asr_{mode}.pid")


def is_process_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def write_pid(mode):
    global pid_file
    pid_file = get_pid_file(mode)
    if os.path.exists(pid_file):
        with open(pid_file) as f:
            old_pid = f.read().strip()
        if old_pid and is_process_alive(old_pid):
            log("错误：已有录制进程在运行，请先停止")
            sys.exit(1)
        try:
            os.remove(pid_file)
        except Exception:
            pass
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))


def remove_pid():
    global pid_file
    if pid_file and os.path.exists(pid_file):
        try:
            os.remove(pid_file)
        except Exception:
            pass


atexit.register(remove_pid)


def pcm_to_wav(pcm_path, wav_path, sample_rate=16000, channels=1, bits=16):
    """给原始 PCM 数据添加 WAV header。"""
    with open(pcm_path, "rb") as f:
        pcm_data = f.read()

    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8

    with open(wav_path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + len(pcm_data)))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<I", 16))
        f.write(struct.pack("<H", 1))
        f.write(struct.pack("<H", channels))
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", bits))
        f.write(b"data")
        f.write(struct.pack("<I", len(pcm_data)))
        f.write(pcm_data)


def mix_pcm(src1, src2, dst_wav):
    """混音两个 s16le mono PCM 文件，输出 WAV。"""
    with open(src1, "rb") as f1, open(src2, "rb") as f2:
        d1 = f1.read()
        d2 = f2.read()

    max_len = max(len(d1), len(d2))
    d1 = d1 + b"\x00" * (max_len - len(d1))
    d2 = d2 + b"\x00" * (max_len - len(d2))

    mixed = bytearray()
    for i in range(0, max_len, 2):
        s1 = struct.unpack("<h", d1[i:i+2])[0]
        s2 = struct.unpack("<h", d2[i:i+2])[0]
        m = s1 + s2
        if m > 32767:
            m = 32767
        elif m < -32768:
            m = -32768
        mixed.extend(struct.pack("<h", m))

    tmp_mix_pcm = dst_wav + ".mix.pcm"
    with open(tmp_mix_pcm, "wb") as f:
        f.write(bytes(mixed))
    pcm_to_wav(tmp_mix_pcm, dst_wav)
    try:
        os.remove(tmp_mix_pcm)
    except Exception:
        pass


def has_audio_activity(pcm_path, check_duration=2.0, threshold=30):
    """检查 PCM 文件最后 check_duration 秒是否有超过 threshold 的音量"""
    if not os.path.exists(pcm_path):
        return False
    file_size = os.path.getsize(pcm_path)
    bytes_per_sec = 16000 * 2  # 16000Hz, 16bit, mono
    check_bytes = int(bytes_per_sec * check_duration)
    start = max(0, file_size - check_bytes)
    start -= start % 2
    if start >= file_size:
        return False
    with open(pcm_path, "rb") as f:
        f.seek(start)
        data = f.read(check_bytes)
    if len(data) < 2:
        return False
    sum_sq = 0
    count = 0
    count = len(data) // 2
    for i in range(count):
        sample = struct.unpack("<h", data[i*2:i*2+2])[0]
        sum_sq += sample * sample
    if count == 0:
        return False
    rms = (sum_sq / count) ** 0.5
    return rms > threshold


async def silence_watcher(pcm_paths, timeout=10):
    """监控 PCM 文件，如果所有文件在 timeout 秒内都没有音频活动，则设置 running = False"""
    global running, stop_reason, record_start_time
    last_active = time.time()
    notified = set()
    try:
        while running:
            await asyncio.sleep(1.0)
            if not running:
                return
            # 前10秒不检测静默，避免ffmpeg启动延迟误判
            if record_start_time and time.time() - record_start_time < 10:
                last_active = time.time()
                continue
            active_flags = [has_audio_activity(p) for p in pcm_paths]
            active = any(active_flags)
            elapsed = int(time.time() - last_active)
            if active:
                last_active = time.time()
                notified.clear()
            else:
                remaining = timeout - elapsed
                for t in [5]:
                    if remaining <= t and t not in notified:
                        notify("录音中", f"静默 {elapsed} 秒，还剩 {t} 秒自动停止")
                        notified.add(t)
                if elapsed > timeout:
                    log(f"检测到静默超过 {timeout} 秒，自动停止")
                    stop_reason = "silence"
                    running = False
                    return
    except asyncio.CancelledError:
        pass


def transcribe(wav_path):
    log("开始转录...")
    with open(wav_path, "rb") as f:
        audio_data = f.read()
    base64_audio = base64.b64encode(audio_data).decode("utf-8")

    headers = {
        "X-Api-App-Key": APPID,
        "X-Api-Access-Key": ACCESS_TOKEN,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
        "Content-Type": "application/json",
    }

    payload = {
        "user": {"uid": APPID},
        "audio": {"data": base64_audio},
        "request": {
            "model_name": "bigmodel",
        },
    }

    payload_json = json.dumps(payload)
    log(f"请求体大小: {len(payload_json)} bytes, audio.data: {len(base64_audio)} chars")

    req = urllib.request.Request(
        API_URL,
        data=payload_json.encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status_code = resp.headers.get("X-Api-Status-Code")
            message = resp.headers.get("X-Api-Message", "")
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        log(f"HTTP 错误: {e.code}")
        body = e.read().decode("utf-8", errors="replace")
        log(f"响应: {body[:500]}")
        return None
    except Exception as e:
        log(f"请求异常: {e}")
        return None

    log(f"API 状态: {status_code} {message}")

    if status_code != "20000000":
        log(f"转录失败: {body[:500]}")
        return None

    try:
        result = json.loads(body)
    except Exception as e:
        log(f"解析响应 JSON 失败: {e}")
        return None

    text = ""
    if "result" in result:
        res = result["result"]
        text = res.get("text", "")
        utterances = res.get("utterances", [])
        if utterances:
            texts = [u.get("text", "") for u in utterances if u.get("text")]
            text = "\n".join(texts)

    return text


async def run(mode: str, out_txt: str):
    global ffmpeg_procs, running, prev_audio_device, stop_reason, record_start_time
    write_pid(mode)
    tmp_wav = None
    stop_reason = "manual"
    record_start_time = None

    try:
        tmp_wav = tempfile.mktemp(suffix=".wav")
        os.makedirs(os.path.dirname(out_txt), exist_ok=True)

        result = subprocess.run(
            ["/opt/homebrew/bin/SwitchAudioSource", "-c"],
            capture_output=True, text=True,
        )
        prev_audio_device = result.stdout.strip()
        log(f"当前音频设备: {prev_audio_device}")
        subprocess.run(
            ["/opt/homebrew/bin/SwitchAudioSource", "-s", "BlackHole + Speakers"],
            capture_output=True,
        )
        log("已切换到 BlackHole + Speakers")

        if mode == "e":
            tmp_pcm = tmp_wav + ".pcm"
            cmd = [
                "/opt/homebrew/bin/ffmpeg", "-y", "-f", "avfoundation",
                "-i", ":0", "-ar", "16000", "-ac", "1",
                "-f", "s16le", tmp_pcm,
            ]
            log("开始录音，按 Ctrl+C 或再次按下快捷键停止...")
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ffmpeg_procs = [p]
            record_start_time = time.time()
            watcher = asyncio.create_task(silence_watcher([tmp_pcm], 10))

            try:
                while running and p.poll() is None:
                    await asyncio.sleep(0.1)
            except Exception:
                pass
            finally:
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass

            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()

            pcm_size = os.path.getsize(tmp_pcm) if os.path.exists(tmp_pcm) else 0
            log(f"PCM 文件大小: {pcm_size} bytes")
            if pcm_size > 0:
                pcm_to_wav(tmp_pcm, tmp_wav)
            try:
                os.remove(tmp_pcm)
            except Exception:
                pass
        else:
            system_pcm = tmp_wav + ".system.pcm"
            mic_pcm = tmp_wav + ".mic.pcm"

            cmd_system = [
                "/opt/homebrew/bin/ffmpeg", "-y", "-f", "avfoundation",
                "-i", ":0", "-ar", "16000", "-ac", "1",
                "-f", "s16le", system_pcm,
            ]
            cmd_mic = [
                "/opt/homebrew/bin/ffmpeg", "-y", "-f", "avfoundation",
                "-i", ":1", "-ar", "16000", "-ac", "1",
                "-f", "s16le", mic_pcm,
            ]

            log("开始录音（系统音频+麦克风），按 Ctrl+C 或再次按下快捷键停止...")
            p1 = subprocess.Popen(cmd_system, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p2 = subprocess.Popen(cmd_mic, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ffmpeg_procs = [p1, p2]
            record_start_time = time.time()
            watcher = asyncio.create_task(silence_watcher([system_pcm], 10))

            try:
                while running and (p1.poll() is None or p2.poll() is None):
                    await asyncio.sleep(0.1)
            except Exception:
                pass
            finally:
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass

            for p in [p1, p2]:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()

            system_size = os.path.getsize(system_pcm) if os.path.exists(system_pcm) else 0
            mic_size = os.path.getsize(mic_pcm) if os.path.exists(mic_pcm) else 0
            log(f"系统音频: {system_size} bytes, 麦克风: {mic_size} bytes")

            # 分别转录两个音轨
            texts = []

            if system_size > 0:
                system_wav = tmp_wav + ".system.wav"
                pcm_to_wav(system_pcm, system_wav)
                t = transcribe(system_wav)
                if t:
                    texts.append(f"【系统音频】\n{t}")
                try:
                    os.remove(system_wav)
                except Exception:
                    pass

            if mic_size > 0:
                mic_wav = tmp_wav + ".mic.wav"
                pcm_to_wav(mic_pcm, mic_wav)
                t = transcribe(mic_wav)
                if t:
                    texts.append(f"【麦克风】\n{t}")
                try:
                    os.remove(mic_wav)
                except Exception:
                    pass

            # 同时生成混音 WAV 用于存档
            if system_size > 0 and mic_size > 0:
                try:
                    mix_pcm(system_pcm, mic_pcm, tmp_wav)
                    log("混音完成")
                except Exception as e:
                    log(f"混音失败: {e}")
                    if system_size > 0:
                        pcm_to_wav(system_pcm, tmp_wav)
                    else:
                        pcm_to_wav(mic_pcm, tmp_wav)
            elif system_size > 0:
                pcm_to_wav(system_pcm, tmp_wav)
            elif mic_size > 0:
                pcm_to_wav(mic_pcm, tmp_wav)

            for f in [system_pcm, mic_pcm]:
                try:
                    os.remove(f)
                except Exception:
                    pass

            # 处理结果并保存
            if texts:
                text = "\n\n".join(texts)
                with open(out_txt, "w", encoding="utf-8") as f:
                    f.write(text + "\n")
                out_wav = os.path.splitext(out_txt)[0] + ".wav"
                if os.path.exists(tmp_wav):
                    shutil.copy(tmp_wav, out_wav)
                log(f"转录结果已保存到: {out_txt}")
                log(f"录音 WAV 已保留到: {out_wav}")
                print("\n=== 转录结果 ===\n")
                print(text)
                notify("转录完成", text)
            else:
                log("转录失败，未写入结果")
                notify("转录失败", "未能获取识别结果，请查看终端输出")

            # mode d 已处理完毕，跳过后续通用逻辑
            return

        if os.path.exists(tmp_wav):
            file_size = os.path.getsize(tmp_wav)
            log(f"录音文件: {tmp_wav}, 大小: {file_size} bytes")
        else:
            log(f"录音文件不存在: {tmp_wav}")

        if prev_audio_device:
            subprocess.run(
                ["/opt/homebrew/bin/SwitchAudioSource", "-s", prev_audio_device],
                capture_output=True,
            )
            log(f"已切回: {prev_audio_device}")

        if not os.path.exists(tmp_wav) or os.path.getsize(tmp_wav) == 0:
            log("录音文件为空，跳过转录")
            notify("录音失败", "录音时间过短或音频设备未就绪，请尝试录制更长时间")
            return

        text = transcribe(tmp_wav)
        if text:
            with open(out_txt, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            out_wav = os.path.splitext(out_txt)[0] + ".wav"
            shutil.copy(tmp_wav, out_wav)
            log(f"转录结果已保存到: {out_txt}")
            log(f"录音 WAV 已保留到: {out_wav}")
            print("\n=== 转录结果 ===\n")
            print(text)
            notify("转录完成", text)
        else:
            log("转录失败，未写入结果")
            notify("转录失败", "未能获取识别结果，请查看终端输出")

    finally:
        if tmp_wav and os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
                log("临时音频已清理")
            except Exception:
                pass
        remove_pid()


def handle_signal(sig, frame):
    global running, record_start_time
    if record_start_time and time.time() - record_start_time < 10:
        notify("录音中", "录音前10秒不能停止")
        return
    running = False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--mode", choices=["e", "d"], default="e")
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log(f"模式: {args.mode}, 输出: {args.output}")
    asyncio.run(run(args.mode, args.output))
