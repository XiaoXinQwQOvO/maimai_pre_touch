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
 │ maitouch (默认)       │   touch 事件      │ App._handle_touch()      │
 └───────────────────────┘                  └──────────────────────────┘
```

同一根 WebSocket 同时承载：WebRTC 信令（offer/answer）、触摸事件、传感器几何同步、心跳/延迟测量。

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
| 输入格式 | `bgra`（NVENC 时） | DXGI 直接给 BGRA，NVENC 在显卡上转 NV12；比在 Python 里 swscale 转 yuv420p 省 **5.3 ms/帧** |
| profile | baseline | 兼容性最好，浏览器 H.264 硬解首选 |
| rc / preset / tune | cbr / p1 / ull | 恒定码率 + 最快档 + 超低延迟（无 B 帧、无前瞻） |
| delay | 0 | 不缓冲帧 |
| 码率 | 9000 kbps（可配） | 960×960@60 的 maimai 画面足够；Wi-Fi 差可降到 4000–6000 |
| time_base | 1/90 | 与 aiortc 的 `MAX_FRAME_RATE` / `VIDEO_TIME_BASE` 保持一致 |

### 两个必须知道的坑

1. **aiortc 的节奏是 30 fps 写死的**：`MediaStreamTrack.next_timestamp()` 里
   `VIDEO_PTIME = 1/30`，与 track 的 `fps` 无关。直接用它，60 fps 的采集会被砍成 30 fps
   （实测 32.9 fps）。所以 `GameVideoTrack.recv()` 自己按 `VIDEO_CLOCK_RATE / fps` 递增 pts 并
   sleep 到目标时刻，落后 1 s 以上时重新对齐而不是补帧。
2. **`dxcam.get_latest_frame()` 会阻塞到下一帧（≤16 ms）**。若直接在主协程里调用，
   事件循环有 80% 时间被占住，WebSocket 触摸/信令要排队。改成 `asyncio.to_thread()` 后
   控制面 RTT 从 0–7 ms（抖动）稳定到 **0–1 ms**。

### 实测（`python tools/selftest.py`，960×960@60，本机）

| 指标 | 值 |
| --- | --- |
| 端到端帧率（模拟手机端收到并解码） | **60.3 fps** |
| 每帧：抓屏 | 14.6 ms（DXGI 垂直同步等待，已被移出事件循环） |
| 每帧：BGRA→编码器输入 | 0.5 ms |
| 每帧：NVENC 编码 | 1.4 ms |
| 每帧总计 | 15.1 ms |
| WebSocket RTT | 0–1 ms |
| 协商编码器 | `H264/90000`（pt 99），实际使用 `H264NvencEncoder` |

## 5. 触摸与传感器映射

用户的硬性约束：**不走鼠标，必须走端口**。因此触摸不是「模拟输入事件」，而是直接构造
游戏从触摸串口读到的状态包。

### 坐标约定

手机端把 `pointerdown/move/up` 的屏幕坐标换算到**视频内容区**（`object-fit: contain` 的黑边已扣除），
再归一化成 0..1 发给 PC。视频内容区 == 抓屏区域 == 游戏客户区（960×960），所以归一化坐标
与游戏里 `add_point(W,H,...)` 用的坐标一一对应，和手机分辨率/横竖屏/黑边完全解耦。

### 传感器几何（单一事实来源：`pc/sensors.py`，`web/app.js` 同步实现）

游戏侧算法（逆向自 `mice.dll`，见 `docs/re_maitouch.md` §6）：

```
u = 2*nx - 1,  v = 2*ny - 1,  r = hypot(u, v),  angle = atan2(v, u)
r < 0.2241                      -> 中心传感器 C
0.2241 <= r < 0.5518            -> 内环 8 个
0.5518 <= r < 1.0               -> 外环 8 个
r >= 1.0                        -> 无传感器（丢弃）
```

* 0° = 画面右侧（3 点方向），随屏幕 y 向下而顺时针；8 个 45° 扇区；
* 每字节 `0x40 | mask`：`0x01` = 该象限靠垂直轴的半区、`0x04` = 靠水平轴的半区，内环左移一位；
* 象限 → 字节：`{0:2, 1:3, 2:4, 3:1}`（byte2 右上、byte3 右下、byte4 左下、byte1 左上），中心 = byte4 bit `0x10`；
* 手机端标号：外环 `A1..A8`、内环 `B1..B8`、中心 `C`（`LABELS`，1 号从 3 点方向顺时针）；
* `snap_to_sensor()`：把触摸吸到该传感器中心（半径取环中点、角度取扇区中心），判定更宽容。

`pc/sensors.py` 与逆向脚本 `tools/maitouch_state.py` 做过交叉验证：2000 个随机触点集合的 14 字节包
**0 处不一致**，101×101 网格 **0 处不一致**。之前把 `0x01/0x04` 写反过，是这套交叉验证抓出来的。

> 早期版本的 5 环 × 8 扇区（`pc/zone_map.py`）是拿到协议细节前的占位实现，已删除：
> 真实设备只有 17 个传感器，多余的环在游戏里不存在。

### 注入后端（`pc/touch_backends.py`）

| 后端 | 实现 | 状态 |
| --- | --- | --- |
| `maitouch` | 冻结 `mice.dll!build_status` + `WriteProcessMemory` 写状态包（`pc/maitouch_port.py`） | ✅ 默认 |
| `log` | 只打日志 | ✅ 干跑 / 联调画面 |
| `synth` | `CreateSyntheticPointerDevice` + `InjectSyntheticPointerInput`（PT_TOUCH） | ⚠️ 不可用（本机注入成功但无 `WM_POINTER` 投递） |

### 端口写入的状态机

```
手机按下 ─► 首次写包时懒冻结 build_status（0x55 -> 0xC3）─► 传感器集合变化时写一次 14 字节
        └► 最后一个触点抬起 ─► 写空闲包 ─► 5 秒无触点 ─► 自动写回 0x55（游戏内鼠标恢复）
```

* 冻结是必须的：`build_status` 每秒被调 ~500 次，每次先用 `0x40` 清包再 OR 鼠标/WM_POINTER 的位，
  不冻结就会被它覆盖；
* 只在「传感器集合变化」时写：设备线程每 ~2 ms 重推完整 14 字节，不需要高频覆写；
* 多押：所有触点 OR 进同一个包，14 字节单次 `WriteProcessMemory` 原子写入；
* 断线保护：WebSocket 断开时服务端释放该连接按住的所有触点，不会留下「鬼按住」；
* 退出/崩溃保护：`atexit` + Ctrl+C 恢复 `0x55` 与空闲包；启动时若发现残留非空闲包会先清掉；
* 恢复命令：`python pc/maitouch_port.py restore`；
* 只对 `maimai_dump_.exe` / `maimai.exe` 打补丁 —— `CameraUploader.exe`、`GmSync.exe` 也加载
  `mice.dll` 并开 COM3，绝不能误伤。

### 验证（`python tools/test_port_backend.py` / `python tools/test_e2e_touch.py`）

游戏运行时实测：

| 检查 | 结果 |
| --- | --- |
| 进程/模块定位（`maimai_dump_.exe` + `mice.dll`，基址运行时解析） | ✅ base `0x69690000` |
| `build_status` 前导 `55 8B EC 83 E4 F8` -> `C3 ...` | ✅ |
| 单区/多区/全 17 区写包与回读逐字节一致 | ✅ 12/12 |
| 游戏自己的鼠标路径写同一块 buffer（按住中心 -> 包内出现 `C`） | ✅ |
| 手机模拟器 -> 服务端 -> 游戏内存（中心/外环/内环/圆外/双指/松手/断线） | ✅ 12/12 |
| 5 秒空闲自动恢复 `0x55` | ✅ |

**未闭环**：游戏是否对这些字节产生判定，需要画面才能确认（`docs/re_maitouch.md` §9）。

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

* 触摸改走 WebRTC DataChannel（不可靠/无序）进一步降延迟；
* 脚本 + 真人鼠标共存：把 `ret` 补丁换成代码洞桩（`call build_status; or byte[packet+1..4],imm; ret`，见 `docs/re_maitouch.md` §7.4）；
* 游戏内判定的最终闭环验证（需要画面比对：按住某传感器看是否出判定）；
* 多手机/平板同时观看（多 PeerConnection 已支持，`self.peers`）。
