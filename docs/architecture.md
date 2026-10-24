# 架构说明

## 1. 目标与约束

* PC 上运行 maimai DX（`G:\Original\maimai`，`maimai.exe`/`maimai_dump_.exe`，由 `mice.exe` 启动）。
* 手机通过局域网当「屏幕 + 触屏」：PC 推画面 → 手机显示 → 手机采集触摸 → 回传 PC → 注入游戏。
* 触摸**不走鼠标**：必须是触摸事件/端口数据这一层。
* 不传音频（声音留在 PC 音箱）。

## 2. 数据流

```
 dxcam(DXGI Duplication)                     aiortc RTCPeerConnection
 ┌───────────────────────┐    BGRA ndarray  ┌──────────────────────────┐
 │ 游戏窗口客户区 960×960 │ ───────────────► │ VideoStreamTrack.recv()   │
 └───────────────────────┘   60 fps         │  ↓ from_ndarray + yuv420p │
                                            │ H264NvencEncoder          │
                                            │  ↓ RTP/H.264 (SRTP/UDP)   │
                                            └────────────┬─────────────┘
                                                         │ Wi-Fi
                                            ┌────────────▼─────────────┐
                                            │ 手机 WebView <video>      │
                                            │ CSS object-fit: contain   │
                                            └────────────┬─────────────┘
                                            pointerdown/move/up
                                            ↓ 归一化 (0..1)
                                            WebSocket JSON
 ┌───────────────────────┐                  ┌──────────────────────────┐
 │ 触摸注入后端           │ ◄─────────────── │ aiohttp /ws              │
 │ synth | log | maitouch│   touch 事件      │ App._handle_touch()      │
 └───────────────────────┘                  └──────────────────────────┘
```

同一根 WebSocket 同时承载：WebRTC 信令（offer/answer）、触摸事件、分区参数同步、心跳/延迟测量。

### 为什么 WebSocket 而不是 DataChannel

* 信令本来就需要一条可靠通道，复用可省一次握手；
* 触摸是低频小包（每次按下 ~60 字节），TCP 回环延迟在局域网内 1–7 ms（实测）；
* 未来若需要更低延迟，可在 `pc/server.py` 的 `_handle_touch` 前加一条 datachannel 旁路，不影响现有结构。

## 3. 抓屏

* `dxcam` 走 DXGI Desktop Duplication：GPU 侧拷贝，不占 CPU，被遮挡/最小化也能抓。
* 区域由 `window.py` 每 0.5 s 轮询一次：按进程名（`maimai.exe`/`maimai_dump_.exe`）或窗口类名（`teaGfx`）定位，
  取 `GetClientRect + ClientToScreen` 得到**客户区**矩形（本项目实测 `800,240,960,960`）。
* 窗口移动/缩放时 `CaptureManager` 自动重启抓屏（间隔 ≥1 s 才检查，避免频繁重建）。
* 进程启动时设为 per-monitor DPI aware，保证窗口矩形是物理像素，与 DXGI 一致。
* 兜底：`GdiCapture`（BitBlt），慢 5–10 ms/帧但从不会因为驱动/显示器配置失败。

实测：960×960 稳定 60.7 fps，帧间隔中位数 16.64 ms，p95 17.28 ms。

## 4. 编码与推流

### 为什么换掉 aiortc 默认编码器

aiortc 的 `H264Encoder._encode_frame` 硬编码 `av.CodecContext.create("libx264", "w")`。
本机 PyAV 17.1.0 的 FFmpeg 构建里已经有 `h264_nvenc`，所以直接继承 `H264Encoder` 重写
`_encode_frame`，改为 NVENC 并给低延迟参数：

```python
CODEC_NAME = "h264_nvenc"
OPTIONS = {"preset": "p1", "tune": "ull", "rc": "cbr", "delay": "0"}
```

接入方式是最小侵入的 monkeypatch：

```python
import aiortc.rtcrtpsender as sender_mod
orig = sender_mod.get_encoder
def patched(codec):
    return H264NvencEncoder() if codec.mimeType.lower() == "video/h264" else orig(codec)
sender_mod.get_encoder = patched
```

`video.install_encoder("auto")` 会先真正开一个 320×320 编码上下文试一遍，失败则继续用 libx264。

实测（960×960，`tools/test_encoder.py`）：平均 **1.41 ms/帧**，p95 1.94 ms，最大 2.23 ms；
H.264 码流用 libav 解码 30/30 帧全部正确；NAL 类型 `SEI/STAP-A/FU-A`，与浏览器兼容。

### 编码参数

| 项 | 值 | 说明 |
| --- | --- | --- |
| profile | baseline | 兼容性最好，浏览器 H.264 硬解首选 |
| rc / preset / tune | cbr / p1 / ull | 恒定码率 + 最快档 + 超低延迟（无 B 帧、无前瞻） |
| delay | 0 | 不缓冲帧 |
| 码率 | 9000 kbps（可配） | 960×960@60 的 maimai 画面足够；Wi-Fi 差可降到 4000–6000 |
| time_base | 1/90 | 与 aiortc 的 `MAX_FRAME_RATE` / `VIDEO_TIME_BASE` 保持一致 |

### 端到端帧率

服务端容量约 60 fps（抓屏 60.7 + 编码 700 fps 余量）。
`tools/test_client.py`（Python/aiortc 软解）实测 32 fps —— 瓶颈是**模拟器端的软件解码**；
真机由浏览器/WebView 硬解，可跑满 60。

## 5. 触摸与分区映射

### 坐标约定

手机端把 `pointerdown/move/up` 的屏幕坐标换算到**视频内容区**（`object-fit: contain` 的黑边已扣除），
再归一化成 0..1 发给 PC。因此与手机分辨率、横竖屏、黑边完全解耦。

### 分区几何（`pc/zone_map.py` 与 `web/app.js` 双侧一致）

* 圆心 `center`（归一化，默认 0.5,0.5）；
* 半径以「画面内切圆」为 1.0：`r = hypot((x-cx)*2, (y-cy)*2)`；
* 5 个环由内到外分界 `ring_edges`（默认 0.34 / 0.5 / 0.66 / 0.82 / 1.0）→ 环名由外到内 A..E；
* 8 个扇区，0° 在 12 点方向、顺时针，`sector_offset_deg` 可整体旋转；
* 分区编号 `A1..E8`（字母 = 环，数字 = 扇区+1）；
* `snap()`：把触摸吸附到所在环的中点半径 + 扇区中心角（判定更宽容）。

> 为什么按几何分区而不是照抄硬件传感器布局：micetools 的 `maitouch` 端口协议里 `k`/`th` 命令只出现
> 索引 0..19（跳过 4/9/14，共 17 对），与常见的 34/40 分区数对不上；在拿到确切传感器映射之前，
> 用几何分区 + 中心吸附是稳妥做法（见 `docs/re_maitouch.md`）。

### 注入后端

| 后端 | 实现 | 状态 |
| --- | --- | --- |
| `synth` | `CreateSyntheticPointerDevice` + `InjectSyntheticPointerInput`（PT_TOUCH，最多 10 点） | ✅ 默认 |
| `log` | 只打日志，不注入 | ✅ 干跑 |
| `maitouch` | 直接写 maitouch 端口对应的进程内触摸状态 | 🚧 逆向中 |

`InjectTouchInput`（旧 API）在 Win11 26200 上恒定返回 0 / `ERROR_INVALID_PARAMETER(87)`，
所以走合成指针设备那条路；两者都属于「真实触摸输入」，不是鼠标模拟。

触点 ID 由后端映射：手机 `pointerId` → 合成指针号 1..10，按下分配、抬起释放，支持多点同时按。

## 6. 手机端

### 为什么 APK 只是 WebView 外壳

页面由 PC 提供（`http://<PC>:8770/app/`），前端改动只要刷新，不用重装 APK；
APK 只做三件浏览器做不好的事：全屏沉浸（隐藏状态栏/导航栏）、屏幕常亮、连不上时改地址。

WebView 关键设置：`setMediaPlaybackRequiresUserGesture(false)`（否则视频不自动播）、
`LOAD_NO_CACHE`（开发期不吃缓存）、`setWebContentsDebuggingEnabled(true)`（可用 chrome://inspect 调试）、
`usesCleartextTraffic="true"`（局域网 http）。

### 前端要点

* `<video muted autoplay playsinline>` + `object-fit: contain`；
* 触摸层与视频同一容器，`touch-action: none`，用 Pointer Events 支持多点；
* `pointermove` 限流 12 ms（≈83 Hz），避免塞满 WebSocket；
* 叠加层用 canvas 画分区网格/触点高亮，`requestAnimationFrame` 驱动；
* 每秒刷新 HUD（分辨率、fps、WS 延迟、触摸计数）；
* 断线自动重连（指数退避 ≤5 s），WebRTC 失败时重建 PeerConnection。

## 7. 免 Gradle 的 APK 构建

`android/build.ps1` 用 build-tools 34.0.0 的原生工具串起来：

```
aapt2 compile --dir res -o resources.zip
aapt2 link --manifest AndroidManifest.xml -R resources.zip -I android.jar --java gen
javac --release 8 -cp android.jar -d classes <sources>
d8 --lib android.jar --min-api 26 --output dex <classes>      → classes.dex
zip  base.apk + classes.dex                                   → unsigned.apk
zipalign -p 4                                                 → aligned.apk
apksigner sign --ks debug.keystore                            → maimai_touchpanel.apk
```

坑：`aapt2` 打不开含非 ASCII 字符的目录。仓库路径若含中文，脚本会自动在
`%LOCALAPPDATA%\maimai_pre_touch_ascii` 建一个 junction 指向仓库，并全程用 ASCII 路径构建；
`.ps1` 文件本身必须保持纯 ASCII（Windows PowerShell 5.1 读无 BOM 的 .ps1 时按 ANSI 解码，中文会被打乱）。

产物：`android/maimai_touchpanel.apk`（约 17 KB，v2/v3 签名，minSdk 26 / targetSdk 34）。

## 8. 延迟预算（目标 1920×1080 手机、5 GHz Wi-Fi）

| 环节 | 典型值 |
| --- | --- |
| 抓屏（DXGI 拷贝，含 1 帧缓冲） | 16 ms |
| NVENC 编码 | 1.4 ms |
| RTP/SRTP + Wi-Fi + 手机硬解 | 15–30 ms |
| 显示（60 Hz vsync） | ≤16 ms |
| 触摸采集 → WebSocket → 注入 | 3–10 ms |
| **合计（画面方向）** | **约 35–65 ms** |

## 9. 后续可做

* `maitouch` 端口级注入后端（等逆向结论）；
* 触摸改走 WebRTC DataChannel（不可靠/无序）进一步降延迟；
* 手机端做「按键音/判定线」提示与自动校准（用游戏内静止画面定位圆环半径）；
* 多手机/平板同时观看（多 PeerConnection 已支持，`self.peers`）。
